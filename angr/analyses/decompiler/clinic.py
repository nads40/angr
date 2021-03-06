
import logging

import networkx

from .. import Analysis, register_analysis
from ...codenode import BlockNode
from ..calling_convention import CallingConventionAnalysis

import ailment


l = logging.getLogger(name=__name__)


class Clinic(Analysis):
    """
    A Clinic deals with AILments.
    """
    def __init__(self, func):

        # Delayed import
        import ailment.analyses  # pylint:disable=redefined-outer-name,unused-import

        self.function = func

        self.graph = None

        self._ail_manager = None
        self._blocks = { }

        # sanity checks
        if not self.kb.functions:
            l.warning('No function is available in kb.functions. It will lead to a suboptimal conversion result.')

        self._analyze()

    #
    # Public methods
    #

    def block(self, addr, size):
        """
        Get the converted block at the given specific address with the given size.

        :param int addr:
        :param int size:
        :return:
        """

        try:
            return self._blocks[(addr, size)]
        except KeyError:
            return None

    def dbg_repr(self):
        """

        :return:
        """

        s = ""

        for block in sorted(self.graph.nodes(), key=lambda x: x.addr):
            s += str(block) + "\n\n"

        return s

    #
    # Private methods
    #

    def _analyze(self):

        CallingConventionAnalysis.recover_calling_conventions(self.project)

        # initialize the AIL conversion manager
        self._ail_manager = ailment.Manager(arch=self.project.arch)

        spt = self._track_stack_pointers()

        self._convert_all()

        self._simplify_blocks(stack_pointer_tracker=spt)

        self._recover_and_link_variables()

        # Make call-sites
        self._make_callsites()

        # Simplify the entire function
        self._simplify_function()

        self._update_graph()

        ri = self.project.analyses.RegionIdentifier(self.function, graph=self.graph)  # pylint:disable=unused-variable

        # print ri.region.dbg_print()

    def _track_stack_pointers(self):
        """
        For each instruction, track its stack pointer offset and stack base pointer offset.

        :return: None
        """

        spt = self.project.analyses.StackPointerTracker(self.function)
        if spt.sp_inconsistent:
            l.warning("Inconsistency found during stack pointer tracking. Decompilation results might be incorrect.")
        return spt

    def _convert_all(self):
        """

        :return:
        """

        for block_node in self.function.graph.nodes():
            ail_block = self._convert(block_node)

            if type(ail_block) is ailment.Block:
                self._blocks[(block_node.addr, block_node.size)] = ail_block

    def _convert(self, block_node):
        """
        Convert a VEX block to an AIL block.

        :param block_node:  A BlockNode instance.
        :return:            An converted AIL block.
        :rtype:             ailment.Block
        """

        if not type(block_node) is BlockNode:
            return block_node

        block = self.project.factory.block(block_node.addr, block_node.size)

        ail_block = ailment.IRSBConverter.convert(block.vex, self._ail_manager)
        return ail_block

    def _simplify_blocks(self, stack_pointer_tracker=None):
        """
        Simplify all blocks in self._blocks.

        :param stack_pointer_tracker:   The StackPointerTracker analysis instance.
        :return:                        None
        """

        # First of all, let's simplify blocks one by one

        for key in self._blocks:
            ail_block = self._blocks[key]
            simplified = self._simplify_block(ail_block, stack_pointer_tracker=stack_pointer_tracker)
            self._blocks[key] = simplified

        # Update the function graph so that we can use reaching definitions
        self._update_graph()

    def _simplify_block(self, ail_block, stack_pointer_tracker=None):
        """
        Simplify a single AIL block.

        :param ailment.Block ail_block: The AIL block to simplify.
        :param stack_pointer_tracker:   The StackPointerTracker analysis instance.
        :return:                        A simplified AIL block.
        """

        simp = self.project.analyses.AILBlockSimplifier(ail_block, stack_pointer_tracker=stack_pointer_tracker)
        return simp.result_block

    def _simplify_function(self):
        """
        Simplify the entire function.

        :return:    None
        """

        # Computing reaching definitions
        rd = self.project.analyses.ReachingDefinitions(func=self.function, func_graph=self.graph, observe_all=True)

        simp = self.project.analyses.AILSimplifier(self.function, func_graph=self.graph, reaching_definitions=rd)

        for key in list(self._blocks.keys()):
            old_block = self._blocks[key]
            if old_block in simp.blocks:
                self._blocks[key] = simp.blocks[old_block]

        self._update_graph()

    def _make_callsites(self):
        """
        Simplify all function call statements.

        :return:    None
        """

        # Computing reaching definitions
        rd = self.project.analyses.ReachingDefinitions(func=self.function, func_graph=self.graph, observe_all=True)

        for key in self._blocks:
            block = self._blocks[key]
            csm = self.project.analyses.AILCallSiteMaker(block, reaching_definitions=rd)
            if csm.result_block:
                ail_block = csm.result_block
                simp = self.project.analyses.AILBlockSimplifier(ail_block)
                self._blocks[key] = simp.result_block

        self._update_graph()

    def _recover_and_link_variables(self):

        # variable recovery
        vr = self.project.analyses.VariableRecoveryFast(self.function, clinic=self, kb=self.kb)  # pylint:disable=unused-variable

        # TODO: The current mapping implementation is kinda hackish...

        for block in self._blocks.values():
            self._link_variables_on_block(block)

    def _link_variables_on_block(self, block):
        """
        Link atoms (AIL expressions) in the given block to corresponding variables identified previously.

        :param ailment.Block block: The AIL block to work on.
        :return:                    None
        """

        variable_manager = self.kb.variables[self.function.addr]

        for stmt_idx, stmt in enumerate(block.statements):
            # I wish I could do functional programming in this method...
            stmt_type = type(stmt)
            if stmt_type is ailment.Stmt.Store:
                # find a memory variable
                mem_vars = variable_manager.find_variables_by_stmt(block.addr, stmt_idx, 'memory')
                if len(mem_vars) == 1:
                    stmt.variable = mem_vars[0][0]
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.data)

            elif stmt_type is ailment.Stmt.Assignment:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.dst)
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.src)

            elif stmt_type is ailment.Stmt.ConditionalJump:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, stmt.condition)

    def _link_variables_on_expr(self, variable_manager, block, stmt_idx, stmt, expr):
        """
        Link atoms (AIL expressions) in the given expression to corresponding variables identified previously.

        :param variable_manager:    Variable manager of the function.
        :param ailment.Block block: AIL block.
        :param int stmt_idx:        ID of the statement.
        :param stmt:                The AIL statement that `expr` belongs to.
        :param expr:                The AIl expression to work on.
        :return:                    None
        """

        if type(expr) is ailment.Expr.Register:
            # find a register variable
            reg_vars = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            # TODO: make sure it is the correct register we are looking for
            if len(reg_vars) == 1:
                reg_var = next(iter(reg_vars))[0]
                expr.variable = reg_var

        elif type(expr) is ailment.Expr.Load:
            # import ipdb; ipdb.set_trace()
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 1:
                var = next(iter(variables))[0]
                expr.variable = var
            else:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.addr)

        elif type(expr) is ailment.Expr.BinaryOp:
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 1:
                var = next(iter(variables))[0]
                expr.referenced_variable = var
            else:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operands[0])
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operands[1])

        elif type(expr) is ailment.Expr.UnaryOp:
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 1:
                var = next(iter(variables))[0]
                expr.referenced_variable = var
            else:
                self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operands)

        elif type(expr) is ailment.Expr.Convert:
            self._link_variables_on_expr(variable_manager, block, stmt_idx, stmt, expr.operand)

        elif isinstance(expr, ailment.Expr.BasePointerOffset):
            variables = variable_manager.find_variables_by_atom(block.addr, stmt_idx, expr)
            if len(variables) == 1:
                var = next(iter(variables))[0]
                expr.referenced_variable = var

    def _update_graph(self):

        node_to_block_mapping = {}
        self.graph = networkx.DiGraph()

        for node in self.function.graph.nodes():
            ail_block = self._blocks.get((node.addr, node.size), node)
            node_to_block_mapping[node] = ail_block

            self.graph.add_node(ail_block)

        for src_node, dst_node, data in self.function.graph.edges(data=True):
            src = node_to_block_mapping[src_node]
            dst = node_to_block_mapping[dst_node]

            self.graph.add_edge(src, dst, **data)


register_analysis(Clinic, 'Clinic')
