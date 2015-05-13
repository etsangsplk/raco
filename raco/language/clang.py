# TODO: To be refactored into shared memory lang,
# where you plugin in the sequential shared memory language specific codegen

from raco import algebra
from raco import expression
from raco.language import clangcommon, Algebra
from raco import rules
from raco.pipelines import Pipelined
from raco.language.clangcommon import StagedTupleRef, CBaseLanguage
from raco.utility import emitlist

from raco.algebra import gensym

import logging

_LOG = logging.getLogger(__name__)

import itertools


class CStagedTupleRef(StagedTupleRef):

    def __additionalDefinitionCode__(self):
        constructor_template = CC.cgenv().get_template(
            'materialized_tuple_ref_additional.cpp')

        numfields = len(self.scheme)

        tupletypename = self.getTupleTypename()
        return constructor_template.render(locals())


class CC(CBaseLanguage):
    _template_path = 'c_templates'
    _cgenv = CBaseLanguage.__get_env_for_template_libraries__(_template_path)

    @classmethod
    def cgenv(cls):
        return cls._cgenv

    @classmethod
    def base_template(cls):
        return cls.cgenv().get_template('base_query.cpp')

    @staticmethod
    def pipeline_wrap(ident, code, attrs):

        # timing code
        if True:
            inner_code = code
            timing_template = \
                CC._cgenv.get_template('clang_pipeline_timing.cpp')

            code = timing_template.render(locals())

        return code

    @staticmethod
    def group_wrap(ident, grpcode, attrs):
        timing_template = CC._cgenv.get_template('clang_group_timing.cpp')
        inner_code = grpcode

        code = timing_template.render(locals())
        return code

    @staticmethod
    def log(txt):
        return """std::cout << "%s" << std::endl;
        """ % txt

    @staticmethod
    def log_unquoted(code, level=0):
        return """std::cout << %s << std::endl;
      """ % code

    @staticmethod
    def log_file(code, level=0):
        return """logfile << "%s" << "\\n";\n """ % code

    @staticmethod
    def log_file_unquoted(code, level=0):
        return """logfile << %s << " ";\n """ % code

    @classmethod
    def compile_stringliteral(cls, s):
        sid = cls.newstringident()
        lookup_init = cls.cgenv().get_template(
            'string_index_lookup.cpp').render(name=sid, st=s)
        build_init = """
        string_index = build_string_index("sp2bench_1m.index");
        """
        return """(%s)""" % sid, [], [build_init, lookup_init]
        # raise ValueError("String Literals not supported\
        # in C language: %s" % s)


class CCOperator(Pipelined, algebra.Operator):
    _language = CC

    @classmethod
    def new_tuple_ref(cls, sym, scheme):
        return CStagedTupleRef(sym, scheme)

    @classmethod
    def language(cls):
        return cls._language

    def postorder_traversal(self, func):
        return self.postorder(func)


from raco.algebra import UnaryOperator


class CMemoryScan(algebra.UnaryOperator, CCOperator):

    def produce(self, state):
        self.input.produce(state)

    # TODO: when have pipeline tree representation,
    # TODO: will have a consumeMaterialized() method instead;
    # TODO: for now we reuse the tuple-based consume
    def consume(self, inputsym, src, state):
        # now generate the scan from memory

        # TODO: generate row variable to avoid naming conflict for nested scans
        memory_scan_template = self.language().cgenv().get_template(
            'memory_scan.cpp')

        stagedTuple = state.lookupTupleDef(inputsym)
        tuple_type = stagedTuple.getTupleTypename()
        tuple_name = stagedTuple.name

        inner_plan_compiled = self.parent().consume(stagedTuple, self, state)

        code = memory_scan_template.render(locals())
        state.setPipelineProperty("type", "in_memory")
        state.addPipeline(code)
        return None

    def num_tuples(self):
        raise NotImplementedError("{}.num_tuples()".format(op=self.opname()))

    def shortStr(self):
        return "%s" % (self.opname())

    def __eq__(self, other):
        """
    For what we are using MemoryScan for, the only use
    of __eq__ is in hashtable lookups for CSE optimization.
    We omit self.schema because the relation_key determines
    the level of equality needed.

    @see FileScan.__eq__
    """
        return UnaryOperator.__eq__(self, other)


class CGroupBy(clangcommon.BaseCGroupby, CCOperator):
    _i = 0

    def __init__(self, *args):
        super(CGroupBy, self).__init__(*args)
        self._cgenv = clangcommon.prepend_template_relpath(
            self.language().cgenv(), '{0}/groupby'.format(CC._template_path))

    @classmethod
    def __genHashName__(cls):
        name = "group_hash_%03d" % cls._i
        cls._i += 1
        return name

    def produce(self, state):
        assert len(self.grouping_list) <= 2, \
            "%s does not currently support groupings of \
            more than 2 attributes" % self.__class__.__name__
        assert len(self.aggregate_list) == 1, \
            """%s currently only supports aggregates of 1 attribute
            (aggregate_list=%s)""" \
            % (self.__class__.__name__, self.aggregate_list)
        for agg_term in self.aggregate_list:
            assert isinstance(agg_term,
                              expression.BuiltinAggregateExpression), \
                """%s only supports simple aggregate expressions.
                A rule should create Apply[GroupBy]""" \
                % self.__class__.__name__

        inp_sch = self.input.scheme()
        self.useMap = len(self.grouping_list) > 0

        if self.useMap:
            if len(self.grouping_list) == 1:
                declr_template = self._cgenv.get_template(
                    '1key_declaration.cpp')
                keytype = self.language().typename(
                    self.grouping_list[0].typeof(
                        inp_sch,
                        None))
            elif len(self.grouping_list) == 2:
                declr_template = self._cgenv.get_template(
                    '2key_declaration.cpp')
                keytypes = ','.join(
                    [self.language().typename(g.typeof(inp_sch, None))
                     for g in self.grouping_list])

        else:
            initial_value = self.__get_initial_value__(
                0,
                cached_inp_sch=inp_sch)
            declr_template = self._cgenv.get_template('0key_declaration.cpp')

        valtype = self.language().typename(
            self.aggregate_list[0].input.typeof(
                inp_sch,
                None))

        self.hashname = self.__genHashName__()
        hashname = self.hashname

        hash_declr = declr_template.render(locals())
        state.addDeclarations([hash_declr])

        my_sch = self.scheme()

        _LOG.debug("aggregates: %s", self.aggregate_list)
        _LOG.debug("columns: %s", self.column_list())
        _LOG.debug("groupings: %s", self.grouping_list)
        _LOG.debug("groupby scheme: %s", my_sch)
        _LOG.debug("groupby scheme[0] type: %s", type(my_sch[0]))

        self.input.produce(state)

        # now that everything is aggregated, produce the tuples
        assert (not self.useMap) \
            or isinstance(self.column_list()[0],
                          expression.AttributeRef), \
            "assumes first column is the key and " \
            "second is aggregate result: %s" % (self.column_list()[0])

        if self.useMap:
            if len(self.grouping_list) == 1:
                produce_template = self._cgenv.get_template('1key_scan.cpp')
            elif len(self.grouping_list) == 2:
                produce_template = self._cgenv.get_template('2key_scan.cpp')
        else:
            produce_template = self._cgenv.get_template('0key_scan.cpp')

        output_tuple = CStagedTupleRef(gensym(), my_sch)
        output_tuple_name = output_tuple.name
        output_tuple_type = output_tuple.getTupleTypename()
        state.addDeclarations([output_tuple.generateDefinition()])

        inner_code = self.parent().consume(output_tuple, self, state)
        code = produce_template.render(locals())
        state.setPipelineProperty("type", "in_memory")
        state.addPipeline(code)

    def consume(self, inputTuple, fromOp, state):
        if self.useMap:
            if len(self.grouping_list) == 1:
                materialize_template = self._cgenv.get_template(
                    '1key_materialize.cpp')
            elif len(self.grouping_list) == 2:
                materialize_template = self._cgenv.get_template(
                    '2key_materialize.cpp')
        else:
            materialize_template = self._cgenv.get_template(
                '0key_materialize.cpp')

        hashname = self.hashname
        tuple_name = inputTuple.name

        # make key from grouped attributes
        if self.useMap:
            inp_sch = self.input.scheme()

            key1pos = self.grouping_list[0].get_position(inp_sch)
            key1val = inputTuple.get_code(key1pos)

            if len(self.grouping_list) == 2:
                key2pos = self.grouping_list[1].get_position(inp_sch)
                key2val = inputTuple.get_code(key2pos)

        if isinstance(self.aggregate_list[0], expression.ZeroaryOperator):
            # no value needed for Zero-input aggregate,
            # but just provide the first column
            valpos = 0
        elif isinstance(self.aggregate_list[0], expression.UnaryOperator):
            # get value positions from aggregated attributes
            valpos = self.aggregate_list[0].input.get_position(self.scheme())
        else:
            assert False, "only support Unary or Zeroary aggregates"

        val = inputTuple.get_code(valpos)

        op = self.aggregate_list[0].__class__.__name__

        code = materialize_template.render(locals())
        return code


class CHashJoin(algebra.Join, CCOperator):
    _i = 0

    @classmethod
    def __genHashName__(cls):
        name = "hash_%03d" % cls._i
        cls._i += 1
        return name

    def __init__(self, *args):
        super(CHashJoin, self).__init__(*args)
        self._cgenv = clangcommon.prepend_template_relpath(
            self.language().cgenv(), '{0}/hashjoin'.format(CC._template_path))

    def produce(self, state):
        if not isinstance(self.condition, expression.EQ):
            msg = "The C compiler can only handle equi-join conditions of \
            a single attribute: %s" % self.condition
            raise ValueError(msg)

        left_sch = self.left.scheme()

        # find the attribute that corresponds to the right child
        self.rightCondIsRightAttr = \
            self.condition.right.position >= len(left_sch)
        self.leftCondIsRightAttr = \
            self.condition.left.position >= len(left_sch)
        assert self.rightCondIsRightAttr ^ self.leftCondIsRightAttr

        # find the attribute that corresponds to the right child
        if self.rightCondIsRightAttr:
            self.right_keypos = \
                self.condition.right.position - len(left_sch)
        else:
            self.right_keypos = \
                self.condition.left.position - len(left_sch)

        # find the attribute that corresponds to the left child
        if self.rightCondIsRightAttr:
            self.left_keypos = self.condition.left.position
        else:
            self.left_keypos = self.condition.right.position

        self.right.childtag = "right"
        # common index is defined by same right side and same key
        hashsym = state.lookupExpr((self.right, self.right_keypos))

        if not hashsym:
            # if right child never bound then store hashtable symbol and
            # call right child produce
            self._hashname = self.__genHashName__()
            _LOG.debug("generate hashname %s for %s", self._hashname, self)
            state.saveExpr((self.right, self.right_keypos), self._hashname)
            self.right.produce(state)
        else:
            # if found a common subexpression on right child then
            # use the same hashtable
            self._hashname = hashsym
            _LOG.debug("reuse hash %s for %s", self._hashname, self)

        self.left.childtag = "left"
        self.left.produce(state)

    def consume(self, t, src, state):
        if src.childtag == "right":
            my_sch = self.scheme()

            declr_template = self._cgenv.get_template("hash_declaration.cpp")

            right_template = self._cgenv.get_template("insert_materialize.cpp")

            hashname = self._hashname
            keypos = self.right_keypos
            keyval = t.get_code(self.right_keypos)

            if self.rightCondIsRightAttr:
                keytype = self.language().typename(
                    self.condition.right.typeof(
                        my_sch,
                        None))
            else:
                keytype = self.language().typename(
                    self.condition.left.typeof(
                        my_sch,
                        None))

            in_tuple_type = t.getTupleTypename()
            in_tuple_name = t.name

            # declaration of hash map
            hashdeclr = declr_template.render(locals())
            state.addDeclarations([hashdeclr])

            # materialization point
            code = right_template.render(locals())

            return code

        if src.childtag == "left":
            left_template = self._cgenv.get_template("lookup.cpp")

            hashname = self._hashname
            keyname = t.name
            keytype = t.getTupleTypename()
            keypos = self.left_keypos
            keyval = t.get_code(keypos)

            right_tuple_name = gensym()

            outTuple = CStagedTupleRef(gensym(), self.scheme())
            out_tuple_type_def = outTuple.generateDefinition()
            out_tuple_type = outTuple.getTupleTypename()
            out_tuple_name = outTuple.name

            state.addDeclarations([out_tuple_type_def])

            inner_plan_compiled = self.parent().consume(outTuple, self, state)

            code = left_template.render(locals())
            return code

        assert False, "src not equal to left or right"


def indentby(code, level):
    indent = " " * ((level + 1) * 6)
    return "\n".join([indent + line for line in code.split("\n")])


# iteration  over table + insertion into hash table with filter

class CUnionAll(clangcommon.CUnionAll, CCOperator):
    pass


class CApply(clangcommon.CApply, CCOperator):
    pass


class CProject(clangcommon.CProject, CCOperator):
    pass


class CSelect(clangcommon.CSelect, CCOperator):
    pass


class CFileScan(clangcommon.CFileScan, CCOperator):

    def __get_ascii_scan_template__(self):
        return CC.cgenv().get_template('ascii_scan.cpp')

    def __get_binary_scan_template__(self):
        # TODO binary input
        return CC.cgenv().get_template('ascii_scan.cpp')

    def __get_relation_decl_template__(self, name):
        return CC.cgenv().get_template('relation_declaration.cpp')


class CStore(clangcommon.BaseCStore, CCOperator):

    def __file_code__(self, t, state):
        code = ""
        state.addPreCode('std::ofstream logfile;\n')
        resultfile = str(self.relation_key).split(":")[2]
        opentuple = 'logfile.open("%s");\n' % resultfile
        schemafile = self.write_schema(self.scheme())
        state.addPreCode(schemafile)
        state.addPreCode(opentuple)

        loggings = emitlist([self.language().log_file_unquoted(
            "{0}".format(t.get_code(i))) for i in range(len(t.scheme))])
        code += loggings

        code += "logfile << '\\n';"

        state.addPostCode('logfile.close();')

        return code

    def write_schema(self, scheme):
        schemafile = 'schema/' + str(self.relation_key).split(":")[2] + \
                     '.schema'
        code = 'logfile.open("%s");\n' % schemafile
        names = [x.encode('UTF8') for x in scheme.get_names()]
        code += self.language().log_file("%s" % names)
        code += self.language().log_file("%s" % scheme.get_types())
        code += 'logfile.close();'
        return code


class CStoreTemp(algebra.StoreTemp, CCOperator):
    def produce(self, state):
        if not state.lookupTempDef(self.name):
            resultsym = gensym()
            stagedTuple = self.new_tuple_ref(resultsym, self.scheme())
            state.addDeclarations([stagedTuple.generateDefinition()])
            if not isinstance(self.input, CMemoryScan):
                state.saveTupleDef(self.name, stagedTuple)
                state.saveTempDef(self.name, resultsym)
                dst_type_name = stagedTuple.getTupleTypename()
                vecdecl = "std::vector<%s> %s;\n" % (dst_type_name, resultsym)
                vecdecl2 = "std::vector<%s> temp;\n" % (dst_type_name)
                state.addDeclarations([vecdecl])
                state.addDeclarations([vecdecl2])
        self.input.produce(state)

    def consume(self, t, src, state):
        code = ""
        if not state.lookupTempDef(self.name):
            dst_name = gensym()
            dst_type_name = t.getTupleTypename()
        else:
            dst_name = state.lookupTempDef(self.name)
            dst_type_name = state.lookupTupleDef(self.name).getTupleTypename()

        code += "temp.push_back(%s);\n" % (t.name)
        state.addPostCode("%s = temp;\n" % (dst_name))
        state.addPostCode("temp.clear();\n")

        if not state.lookupTempDef(self.name):
            state.saveTupleDef(self.name, t)
            state.saveTempDef(self.name, dst_name)
            vecdecl = "std::vector<%s> %s;\n" % (dst_type_name, dst_name)
            vecdecl2 = "std::vector<%s> temp;\n" % (dst_type_name)
            state.addDeclarations([vecdecl])
            state.addDeclarations([vecdecl2])

        return code


class CScanTemp(algebra.ScanTemp, CCOperator):
    def produce(self, state):
        inputsym = state.lookupTempDef(self.name)
        stagedTuple = state.lookupTupleDef(self.name)
        tuple_type = stagedTuple.getTupleTypename()
        tuple_name = stagedTuple.name
        memory_scan_template = CC.cgenv().get_template(
            'memory_scan.cpp')
        inner_plan_compiled = self.parent().consume(stagedTuple, self, state)

        code = memory_scan_template.render(locals())
        state.setPipelineProperty("type", "in_memory")
        state.addPipeline(code)

    def consume(self, t, src, state):
        return ''


class CSingletonRelation(algebra.SingletonRelation, CCOperator):
    def produce(self, state):
        resultsym = gensym()
        state.saveExpr(self, resultsym)
        stagedTuple = self.new_tuple_ref(resultsym, self.scheme())
        state.addDeclarations([stagedTuple.generateDefinition()])
        state.saveTupleDef(resultsym, stagedTuple)
        state.setPipelineProperty("type", "singleton")
        code = self.parent().consume(stagedTuple, self, state)
        state.setPipelineProperty("type", "in_memory")
        state.addPipeline(code)

    def consume(self, t, src, state):
        return ''


class CCrossProduct(algebra.CrossProduct, CCOperator):
    def __init__(self):
        super(CCrossProduct, self).__init__()
        self.singleton = None

    def produce(self, state):
        stagedTuple = self.new_tuple_ref(gensym(), self.scheme())
        state.addDeclarations([stagedTuple.generateDefinition()])
        state.saveTupleDef(self.left, stagedTuple)
        state.saveTupleDef(self.right, stagedTuple)
        self.left.produce(state)
        self.right.produce(state)
        c = self.left
        while (self.singleton is None and c):
            for child in c.children():
                if isinstance(child, CSingletonRelation):
                    self.singleton = self.left
                else:
                    c = child.children()
        if self.singleton is None:
            while (self.singleton is None and c):
                for child in c.children():
                    if isinstance(child, CSingletonRelation):
                        self.singleton = self.right
                    else:
                        c = child.children()
        assert self.singleton is not None, "Only cross product with singleton"

    def consume(self, t, src, state):
        code = ""
        if src == self.singleton:
            pass
        else:
            pass
        return code


class MemoryScanOfFileScan(rules.Rule):

    """A rewrite rule for making a scan into
    materialization in memory then memory scan"""

    def fire(self, expr):
        if isinstance(expr, algebra.Scan) and not isinstance(expr, CFileScan):
            return CMemoryScan(CFileScan(expr.relation_key, expr.scheme()))
        return expr

    def __str__(self):
        return "Scan => MemoryScan[FileScan]"


def clangify(emit_print):
    return [
        rules.ProjectingJoinToProjectOfJoin(),

        rules.OneToOne(algebra.Select, CSelect),
        MemoryScanOfFileScan(),
        rules.OneToOne(algebra.Apply, CApply),
        rules.OneToOne(algebra.Join, CHashJoin),
        rules.OneToOne(algebra.GroupBy, CGroupBy),
        rules.OneToOne(algebra.Project, CProject),
        rules.OneToOne(algebra.UnionAll, CUnionAll),
        rules.OneToOne(algebra.StoreTemp, CStoreTemp),
        rules.OneToOne(algebra.ScanTemp, CScanTemp),
        # TODO: obviously breaks semantics
        rules.OneToOne(algebra.Union, CUnionAll),
        rules.OneToOne(algebra.SingletonRelation, CSingletonRelation),
        rules.OneToOne(algebra.CrossProduct, CCrossProduct),
        clangcommon.StoreToBaseCStore(emit_print, CStore),
        clangcommon.BreakHashJoinConjunction(CSelect, CHashJoin)
    ]


class CCAlgebra(Algebra):

    def __init__(self, emit_print=clangcommon.EMIT_CONSOLE):
        """ To store results into a file or onto console """
        self.emit_print = emit_print

    def opt_rules(self, **kwargs):
        # Sequence that works for datalog
        # TODO: replace with below
        # datalog_rules = [
        # rules.CrossProduct2Join(),
        # rules.SimpleGroupBy(),
        # rules.OneToOne(algebra.Select, CSelect),
        # MemoryScanOfFileScan(),
        # rules.OneToOne(algebra.Apply, CApply),
        # rules.OneToOne(algebra.Join, CHashJoin),
        # rules.OneToOne(algebra.GroupBy, CGroupBy),
        # rules.OneToOne(algebra.Project, CProject),
        # TODO: obviously breaks semantics
        # rules.OneToOne(algebra.Union, CUnionAll),
        # rules.FreeMemory()
        # ]

        # sequence that works for myrial
        rule_grps_sequence = [
            rules.remove_trivial_sequences,
            rules.simple_group_by,
            clangcommon.clang_push_select,
            [rules.ProjectToDistinctColumnSelect(),
             rules.JoinToProjectingJoin()],
            rules.push_apply,
            clangify(self.emit_print)
        ]

        if kwargs.get('SwapJoinSides'):
            rule_grps_sequence.insert(0, [rules.SwapJoinSides()])

        return list(itertools.chain(*rule_grps_sequence))
