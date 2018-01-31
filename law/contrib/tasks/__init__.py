# -*- coding: utf-8 -*-

"""
Tasks that provide common and often used functionality.
"""


__all__ = ["TransferLocalFile"]


import os
from collections import OrderedDict
from abc import abstractmethod

import luigi
import six

from law.task.base import Task
from law.workflow.local import LocalWorkflow
from law.target.file import FileSystemTarget
from law.target.local import LocalFileTarget
from law.target.collection import TargetCollection, SiblingFileCollection
from law.parameter import NO_INT
from law.decorator import log
from law.util import flatten, iter_chunks


class TransferLocalFile(Task):

    source_path = luigi.Parameter(description="path to the file to transfer")
    replicas = luigi.IntParameter(default=0, description="number of replicas to generate, uses "
        "replica_format when > 0 for creating target basenames, default: 0")

    replica_format = "{name}.{i}{ext}"

    exclude_db = True

    def get_source_target(self):
        # when self.source_path is set, return a target around it
        # otherwise assume self.requires() returns a task with a single local target
        return LocalFileTarget(self.source_path) if self.source_path else self.input()

    @abstractmethod
    def single_output(self):
        pass

    def output(self):
        output = self.single_output()
        if self.replicas <= 0:
            return output

        # prepare replica naming
        name, ext = os.path.splitext(output.basename)
        basename = lambda i: self.replica_format.format(name=name, ext=ext, i=i)

        # return the replicas in a SiblingFileCollection
        output_dir = output.parent
        return SiblingFileCollection([
            output_dir.child(basename(i), "f") for i in six.moves.range(self.replicas)
        ])

    @log
    def run(self):
        self.transfer(self.get_source_target())

    def transfer(self, src_path):
        output = self.output()

        # single output or replicas?
        if not isinstance(output, SiblingFileCollection):
            output.copy_from_local(src_path, cache=False)
        else:
            # upload all replicas
            progress_callback = self.create_progress_callback(self.replicas)
            for i, replica in enumerate(output.targets):
                replica.copy_from_local(src_path, cache=False)
                progress_callback(i)
                self.publish_message("uploaded {}".format(replica.basename))


class CascadeMerge(Task, LocalWorkflow):

    cascade_tree = luigi.IntParameter(default=0, description="the index of the cascade tree, in "
        "case multiple trees are used, default: 0")
    cascade_depth = luigi.IntParameter(default=0, description="the depth of this workflow in the "
        "cascade tree with 0 being the root of the tree, default: 0")
    keep_nodes = luigi.BoolParameter(significant=False, description="keep merged results from "
        "intermediary nodes in the cascade cache directory")

    # internal parameter
    n_cascade_leaves = luigi.IntParameter(default=NO_INT, significant=False)

    # fixate some workflow parameters
    acceptance = 1.
    tolerance = 0.
    pilot = False

    node_format = "{name}.t{tree}.d{depth}.b{branch}{ext}"
    merge_factor = 2

    exclude_params_db = {"n_cascade_leaves"}

    exclude_db = True

    def __init__(self, *args, **kwargs):
        super(CascadeMerge, self).__init__(*args, **kwargs)

        # cache values from expensive computations
        if self.is_workflow():
            self._cascade_trees = None
            self._leaves_per_tree = None

    @property
    def cascade_trees(self):
        # let the workflow create the cascade trees, branches can simply refer to them
        if self.is_workflow():
            if not self._cascade_trees:
                self._build_cascade_trees()
            return self._cascade_trees
        else:
            return self.as_workflow().cascade_trees

    @property
    def leaves_per_tree(self):
        if self.is_workflow():
            return self._leaves_per_tree
        else:
            return self.as_workflow().leaves_per_tree

    def _build_cascade_trees(self):
        # a node in the tree can be described by a tuple of integers, where each value denotes the
        # branch path to go down the tree to reach the node (e.g. (2, 0) -> 2nd branch, 0th branch),
        # so the length of the tuple defines the depth of the node via ``depth = len(node) - 1``
        # the tree itself is a dict that maps depths to lists of nodes with that depth
        # when more than one tree is used, each simply handles ``n_leaves / n_trees`` leaves

        # helper to convert nested lists of leaf number chunks into a list of nodes in the format
        # described above
        def nodify(obj, node=None):
            if not isinstance(obj, list):
                return []
            nodes = []
            if node is None:
                node = tuple()
            else:
                nodes.append(node)
            for i, _obj in enumerate(obj):
                nodes += nodify(_obj, node + (i,))
            return nodes

        # first, determine the number of files to merge in total when not already set via params
        if self.n_cascade_leaves == NO_INT:
            # get inputs, i.e. outputs of workflow requirements and trace actual inputs to merge
            inputs = luigi.task.getpaths(self.cascade_workflow_requires())
            if isinstance(inputs, (tuple, list)) and len(inputs) == 2 and callable(inputs[1]):
                inputs, tracer = inputs
                inputs = tracer(inputs)
            self.n_cascade_leaves = len(inputs)

        # infer the number of trees from the cascade output
        output = self.cascade_output()
        n_trees = 1 if not isinstance(output, TargetCollection) else len(output)

        # determine the number of leaves per tree
        leaves_per_tree = n_trees * [self.n_cascade_leaves // n_trees]
        for i in six.moves.range(self.n_cascade_leaves % n_trees):
            leaves_per_tree[i] += 1

        # build trees
        trees = []
        for n_leaves in leaves_per_tree:
            # build a nested list of leaf numbers using the merge factor
            # e.g. 9 leaves with factor 3 -> [[0, 1, 2], [3, 4, 5], [6, 7, 8]]
            # TODO: this point defines the actual tree structure, which is bottom-up at the moment,
            # but maybe it's good to configure this
            nested_leaves = list(six.moves.range(n_leaves))
            while len(nested_leaves) > 1:
                nested_leaves = list(iter_chunks(nested_leaves, self.merge_factor))

            # convert the list of nodes to the tree format described above
            tree = {}
            for node in nodify(nested_leaves):
                depth = len(node) - 1
                tree.setdefault(depth, []).append(node)

            trees.append(tree)

        # store values
        self._leaves_per_tree = leaves_per_tree
        self._cascade_trees = trees

    def create_branch_map(self):
        tree = self.cascade_trees[self.cascade_tree]
        nodes = tree[self.cascade_depth]
        return dict(enumerate(nodes))

    @property
    def is_root(self):
        return self.depth == 0

    @property
    def is_leaf(self):
        tree = self.cascade_trees[self.cascade_tree]
        max_depth = max(tree.keys())
        return self.cascade_depth == max_depth

    @abstractmethod
    def cascade_requires(self):
        # should return a tuple containing
        # 1) the leaf requirements of a cascading task branch
        # 2) (opt.) a function that takes the outputs of the requirements and returns the actual
        # targets to merge (e.g. usefull when the requirements output multiple targets)
        pass

    @abstractmethod
    def cascade_workflow_requires(self):
        # should return a tuple containing
        # 1) the leaf requirements of a cascading task workflow
        # 2) (opt.) a function that takes the outputs of the requirements and returns the actual
        # targets to merge (e.g. usefull when the requirements output multiple targets)
        pass

    @abstractmethod
    def cascade_output(self):
        # this should return a single target to explicitely denote a single tree
        # or a target collection whose targets are accessible as items via the tree numbers
        pass

    @abstractmethod
    def merge(self, inputs, output):
        pass

    def workflow_requires(self):
        reqs = super(CascadeMerge, self).workflow_requires()

        if self.is_leaf:
            # this is simply the cascade requirement
            reqs["cascade"] = self.cascade_workflow_requires()

        else:
            # not a leaf, just require the next cascade depth
            reqs["cascade"] = self.req(self, depth=self.depth + 1)

        return reqs

    def requires(self):
        reqs = OrderedDict()

        if self.is_leaf:
            # this is simply the cascade requirement
            # also determine and pass the corresponding leaf number range which is rather tricky
            # strategy: consider the node tuple values as a number in a numeral system where the
            # base corresponds to our merge factor, convert it to a decimal number and account for
            # the offset from previous trees
            offset = sum(self.leaves_per_tree[:self.cascade_tree])
            node = self.branch_value
            value = "".join(str(v) for v in node)
            start_branch = offset + self.merge_factor * int(value, self.merge_factor)
            end_branch = min(start_branch + self.merge_factor, self.n_cascade_leaves)
            reqs["cascade"] = self.cascade_requires(start_branch, end_branch)

        else:
            # get all child nodes in the next layer at depth = depth + 1, store their branches
            # note: child node tuples contain the exact same values plus an additional one
            node = self.branch_value
            tree = self.cascade_trees[self.cascade_tree]
            branches = [i for i, n in enumerate(tree[self.depth + 1]) if n[:-1] == node]

            # add to requirements
            reqs["cascade"] = {b: self.req(self, branch=b, depth=self.depth + 1) for b in branches}

        return reqs

    def cascade_cache_directory(self):
        # by default, use the targets parent directory, also for SinglingFileCollections
        # otherwise, no default decision is implemented
        output = self.cascade_output()
        if isinstance(output, FileSystemTarget):
            return output.parent
        elif isinstance(output, SiblingFileCollection):
            return output.dir
        else:
            raise NotImplementedError("{}.cascade_cache_directory is not implemented".format(
                self.__class__.__name__))

    def output(self):
        output = self.cascade_output()
        if self.is_root:
            if isinstance(output, TargetCollection):
                return output.targets[self.cascade_tree]
            else:
                return output
        else:
            name, ext = os.path.splitext(output.basename)
            basename = self.node_format.format(name=name, ext=ext, branch=self.branch,
                tree=self.cascade_tree, depth=self.cascade_depth)
            return self.cascade_cache_directory().child(basename, "f")

    @log
    def run(self):
        # trace actual inputs to merge
        inputs = self.input()["cascade"]
        if self.is_leaf:
            if isinstance(inputs, (tuple, list)) and len(inputs) == 2 and callable(inputs[1]):
                inputs, tracer = inputs
                inputs = tracer(inputs)

        # flatten inputs
        if isinstance(inputs, TargetCollection):
            inputs = flatten(inputs.targets)
        else:
            inputs = flatten(inputs)

        # merge
        self.publish_message("start merging of node {} in tree {}".format(self.branch_value,
            self.cascade_tree))
        self.merge(inputs, self.output())

        # remove intermediate nodes
        if not self.is_leaf and not self.keep_nodes:
            self.publish_message("remove intermediate results to node {} in tree {}".format(
                self.branch_value, self.cascade_tree))
            for target in inputs:
                target.remove()
