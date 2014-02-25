# TODO: To be refactored into parallel shared memory lang,
# where you plugin in the parallel shared memory language specific codegen

from raco import algebra
from raco import expression
from raco import catalog
from raco.language import Language
from raco import rules
from raco.utility import emitlist
from raco.pipelines import Pipelined
from raco.clangcommon import StagedTupleRef
from raco import clangcommon

from algebra import gensym

import logging
LOG = logging.getLogger(__name__)

import os.path

template_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "grappa_templates")

def readtemplate(fname):
    return file(os.path.join(template_path, fname)).read()


base_template = readtemplate("base_query.template")

class GrappaStagedTupleRef(StagedTupleRef):
  def __afterDefinitionCode__(self):
     # Grappa requires structures to be block aligned if they will be
     # iterated over with localizing forall
     return "GRAPPA_BLOCK_ALIGNED"
    

class GrappaLanguage(Language):
    @classmethod
    def new_relation_assignment(cls, rvar, val):
        return """
    %s
    %s
    """ % (cls.relation_decl(rvar), cls.assignment(rvar, val))

    @classmethod
    def relation_decl(cls, rvar):
        return "GlobalAddress<Tuple> %s;" % rvar

    @classmethod
    def assignment(cls, x, y):
        return "%s = %s;" % (x, y)

    @staticmethod
    def initialize(resultsym):
        return ""


    @staticmethod
    def body(compileResult, resultsym):
      queryexec = compileResult.getExecutionCode()
      initialized = compileResult.getInitCode()
      declarations = compileResult.getDeclCode()
      return base_template % locals()

    @staticmethod
    def finalize(resultsym):
        return ""

    @staticmethod
    def log(txt):
        return  """LOG(INFO) << "%s";\n""" % txt
    
    @staticmethod
    def log_unquoted(code): 
      return """LOG(INFO) << %s;\n""" % code

    @staticmethod
    def comment(txt):
        return  "// %s\n" % txt

    nextstrid = 0
    @classmethod
    def newstringident(cls):
        r = """str_%s""" % (cls.nextstrid)
        cls.nextstrid += 1
        return r

    @classmethod
    def compile_numericliteral(cls, value):
        return '%s'%(value), []

    @classmethod
    def compile_stringliteral(cls, s):
        sid = cls.newstringident()
        init = """auto %s = string_index.string_lookup("%s");""" % (sid, s)
        return """(%s)""" % sid, [init]
        #raise ValueError("String Literals not supported in C language: %s" % s)

    @classmethod
    def negation(cls, input):
        innerexpr, inits = input
        return "(!%s)" % (innerexpr,), inits

    @classmethod
    def boolean_combine(cls, args, operator="&&"):
        opstr = " %s " % operator
        conjunc = opstr.join(["(%s)" % arg for arg, _ in args])
        inits = reduce(lambda sofar, x: sofar+x, [d for _, d in args])
        LOG.debug("conjunc: %s", conjunc)
        return "( %s )" % conjunc, inits

    @classmethod
    def compile_attribute(cls, expr):
        if isinstance(expr, expression.NamedAttributeRef):
            raise TypeError("Error compiling attribute reference %s. C compiler only support unnamed perspective.  Use helper function unnamed." % expr)
        if isinstance(expr, expression.UnnamedAttributeRef):
            symbol = expr.tupleref.name
            position = expr.position # NOTE: this will only work in Selects right now
            return '%s.get(%s)' % (symbol, position), []

class GrappaOperator (Pipelined):
    language = GrappaLanguage
    
class MemoryScan(algebra.Scan, GrappaOperator):
  def produce(self, state):
    code = ""
    #generate the materialization from file into memory
    #TODO split the file scan apart from this in the physical plan

    #TODO for now this will break whatever relies on self.bound like reusescans
    #Scan is the only place where a relation is declared
    resultsym = gensym()
    
    # now generate the scan from memory
    inputsym = resultsym

# scan from index
#    memory_scan_template = """forall_localized( %(inputsym)s_index->vs, %(inputsym)s_index->nv, [](int64_t ai, Vertex& a) {
#      forall_here_async<&impl::local_gce>( 0, a.nadj, [=](int64_t start, int64_t iters) {
#      for (int64_t i=start; i<start+iters; i++) {
#        auto %(tuple_name)s = a.local_adj[i];
#          
#          %(inner_plan_compiled)s
#       } // end scan over %(inputsym)s (for)
#       }); // end scan over %(inputsym)s (forall_here_async)
#       }); // end scan over %(inputsym)s (forall_localized)
#       """

    memory_scan_template = """start = walltime();
forall( %(inputsym)s.data, %(inputsym)s.numtuples, [=](int64_t i, %(tuple_type)s& %(tuple_name)s) {
%(inner_plan_compiled)s
}); // end  scan over %(inputsym)s
end = walltime();
in_memory_runtime += (end-start);
"""

    rel_decl_template = """Relation<%(tuple_type)s> %(resultsym)s;"""
    
    stagedTuple = GrappaStagedTupleRef(inputsym, self.scheme())

    tuple_type_def = stagedTuple.generateDefition()
    tuple_type = stagedTuple.getTupleTypename()
    tuple_name = stagedTuple.name

    # generate code for actual IO and materialization in memory
    code += FileScan(self.relation_key, self._scheme).compileme(resultsym, tuple_type)
    
    # generate declaration of the in-memory relation
    rel_decl = rel_decl_template % locals()
    state.addDeclarations([tuple_type_def, rel_decl])

    inner_plan_compiled = self.parent.consume(stagedTuple, self)

    code += memory_scan_template % locals()
    state.addPipeline(code)
    
  def consume(self, t, src, state):
    assert False, "as a source, no need for consume"
    
    

class FileScan(algebra.Scan):

    def compileme(self, resultsym, tuple_typename):
        # TODO use the identifiers (don't split str and extract)
        LOG.debug('compiling file scan for relation_key %s' % self.relation_key)
        name = str(self.relation_key).split(':')[2]


        #tup = (resultsym, self.originalterm.originalorder, self.originalterm)
        #self.trace("// Original query position of %s: term %s (%s)" % tup)
        
        #TODO: manage the concurrent synchronization objects
        
        # graph-specific file scan
        ascii_scan_template_GRAPH = """
          {
            tuple_graph tg;
            tg = readTuples( "%(name)s" );
            
            FullEmpty<GlobalAddress<Graph<Vertex>>> f1;
            privateTask( [&f1,tg] {
              f1.writeXF( Graph<Vertex>::create(tg, /*directed=*/true) );
            });
            auto l_%(resultsym)s_index = f1.readFE();
            
            on_all_cores([=] {
              %(resultsym)s_index = l_%(resultsym)s_index;
            });
        }
        """
        
        ascii_scan_template = """
        start = walltime();
        {
        auto t = readTuples<%(tuple_typename)s>( "%(name)s", FLAGS_nt);
        Relation<%(tuple_typename)s> l_%(resultsym)s;
        l_%(resultsym)s.data = t;
        l_%(resultsym)s.numtuples = FLAGS_nt;
        on_all_cores([=]{ %(resultsym)s = l_%(resultsym)s; });
        }
        end = walltime();
        scan_runtime += (end-start);
        """

        if isinstance(self.relation_key, catalog.ASCIIFile):
            code = ascii_scan_template % locals()
        else:
            LOG.info("binary not currently supported for GrappaLanguage, emitting ascii")
            code = ascii_scan_template % locals()

        return code
      
    def __str__(self):
      return "%s(%s)" % (self.opname(), self.relation_key)


def getTaggingFunc(t):
  """ 
  Return a visitor function that will tag 
  UnnamedAttributes with the provided TupleRef
  """

  def tagAttributes(expr):
    # TODO non mutable would be nice
    if isinstance(expr, expression.UnnamedAttributeRef):
      expr.tupleref = t

    return None
  
  return tagAttributes



class HashJoin(algebra.Join, GrappaOperator):
  _i = 0

  @classmethod
  def __genHashName__(cls):
    name = "hash_%03d" % cls._i;
    cls._i += 1
    return name
  
  def produce(self, state):
    if not isinstance(self.condition, expression.EQ):
      msg = "The C compiler can only handle equi-join conditions of a single attribute: %s" % self.condition
      raise ValueError(msg)
    
    self._hashname = self.__genHashName__()
    self.outTuple = GrappaStagedTupleRef(gensym(), self.scheme())
    
    self.right.childtag = "right"
    init_template = """%(hashname)s.init_global_DHT( &%(hashname)s, 64 );""" 
    setro_template = """%(hashname)s.set_RO_global( &%(hashname)s );"""
    hashname = self._hashname

    # surround the code for the pipeline with hash initialization and setRO
    state.addCode(init_template%locals())
    self.right.produce(state)
    state.addCode(setro_template%locals())

    self.left.childtag = "left"
    self.left.produce(state)


  def consume(self, t, src, state):
    if src.childtag == "right":
      declr_template =  """typedef MatchesDHT<int64_t, %(in_tuple_type)s, identity_hash> DHT_%(in_tuple_type)s;
      DHT_%(in_tuple_type)s %(hashname)s;
      """
      
      right_template = """%(hashname)s.insert(%(keyname)s.get(%(keypos)s), %(keyname)s);
      """   
      
      hashname = self._hashname
      keyname = t.name
      keypos = self.condition.right.position-len(self.left.scheme())
      
      out_tuple_type_def = self.outTuple.generateDefition()
      self.rightTuple = t #TODO: this induces a right->left dependency
      in_tuple_type = self.rightTuple.getTupleTypename()

      # declaration of hash map
      hashdeclr =  declr_template % locals()
      state.addDeclarations([out_tuple_type_def, hashdeclr])
      
      # materialization point
      code = right_template % locals()
      
      return code
    
    if src.childtag == "left":
      left_template = """
      %(hashname)s.lookup_iter( %(keyname)s.get(%(keypos)s), [=](%(right_tuple_type)s& %(right_tuple_name)s) {
        %(out_tuple_type)s %(out_tuple_name)s = combine<%(out_tuple_type)s, %(keytype)s, %(right_tuple_type)s> (%(keyname)s, %(right_tuple_name)s);
        %(inner_plan_compiled)s
      });
    """

      hashname = self._hashname
      keyname = t.name
      keytype = t.getTupleTypename()
      keypos = self.condition.left.position
      
      right_tuple_type = self.rightTuple.getTupleTypename()
      right_tuple_name = self.rightTuple.name

      # or could make up another name
      #right_tuple_name = GrappaStagedTupleRef.genname() 

      out_tuple_type = self.outTuple.getTupleTypename()
      out_tuple_name =self.outTuple.name
      
      inner_plan_compiled = self.parent.consume(self.outTuple, self, state)
      
      code = left_template % locals()
      return code

    assert False, "src not equal to left or right"
      


def compile_filtering_join(joincondition, leftcondition, rightcondition):
    """return code for processing a filtering join"""


def indentby(code, level):
    indent = " " * ((level + 1) * 6)
    return "\n".join([indent + line for line in code.split("\n")])



#
#
#
#class FreeMemory(GrappaOperator):
#  def fire(self, expr):
#    for ref in noReferences(expr)


# Basic selection like serial C++      
class GrappaSelect(clangcommon.CSelect, GrappaOperator): pass

# Basic apply like serial C++
class GrappaApply(clangcommon.CApply, GrappaOperator): pass

# Basic duplication based bag union like serial C++
class GrappaUnionAll(clangcommon.CUnionAll, GrappaOperator): pass

# Basic materialized copy based project like serial C++
class GrappaProject(clangcommon.CProject, GrappaOperator): pass

    
class swapJoinSides(rules.Rule):
  # swaps the inputs to a join
  def fire(self, expr):
    if isinstance(expr,algebra.Join):
      return algebra.Join(expr.condition, expr.right, expr.left)
    else:
      return expr

class GrappaAlgebra(object):
    language = GrappaLanguage

    operators = [
    #FileScan,
    MemoryScan,
    GrappaSelect,
    GrappaApply,
    GrappaProject,
    GrappaUnionAll,
    HashJoin
  ]
    rules = [
  #  rules.removeProject(),
    rules.CrossProduct2Join(),
    #swapJoinSides(),
    rules.OneToOne(algebra.Select,GrappaSelect),
    rules.OneToOne(algebra.Apply, GrappaApply),
    rules.OneToOne(algebra.Scan,MemoryScan),
    rules.OneToOne(algebra.Join,HashJoin),
    rules.OneToOne(algebra.Project, GrappaProject),
    rules.OneToOne(algebra.Union,GrappaUnionAll) #TODO: obviously breaks semantics
  #  rules.FreeMemory()
  ]
