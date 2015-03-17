# This file is part of PyOP2
#
# PyOP2 is Copyright (c) 2012, Imperial College London and
# others. Please see the AUTHORS file in the main source directory for
# a full list of copyright holders.  All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions
# are met:
#
#     * Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above copyright
#       notice, this list of conditions and the following disclaimer in the
#       documentation and/or other materials provided with the distribution.
#     * The name of Imperial College London or that of other
#       contributors may not be used to endorse or promote products
#       derived from this software without specific prior written
#       permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTERS
# ''AS IS'' AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDERS OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT,
# INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
# (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED
# OF THE POSSIBILITY OF SUCH DAMAGE.

"""OP2 backend for fusion and tiling of ``ParLoops``."""

from contextlib import contextmanager
from collections import OrderedDict
from copy import deepcopy as dcopy
import os

from base import *
import base
import compilation
import host
from backends import _make_object
from caching import Cached
from profiling import lineprof, timed_region, profile
from logger import warning, info as log_info
from mpi import collective
from configuration import configuration
from utils import flatten, strip, as_tuple

import coffee
from coffee import base as ast
from coffee.utils import visit as ast_visit, \
    ast_update_id as ast_update_id, ast_c_make_alias as ast_make_alias

import slope_python as slope


class Arg(host.Arg):

    @staticmethod
    def specialize(args, gtl_map, loop_id):
        """Given ``args``, instances of some :class:`fusion.Arg` superclass,
        create and return specialized :class:`fusion.Arg` objects.

        :param args: either a single :class:`host.Arg` object or an iterator
                     (accepted: list, tuple) of :class:`host.Arg` objects.
        :gtl_map: a dict associating global maps' names to local maps' c_names.
        :param loop_id: indicates the position of the args` loop in the loop
                        chain
        """

        def convert(arg, gtl_map, loop_id):
            # Retrive local maps
            maps = as_tuple(arg.map, Map)
            c_local_maps = [None]*len(maps)
            for i, map in enumerate(maps):
                c_local_maps[i] = [None]*len(map)
                for j, m in enumerate(map):
                    c_local_maps[i][j] = gtl_map["%s%d_%d" % (m.name, i, j)]
            # Instantiate and initialize new, specialized Arg
            _arg = Arg(arg.data, arg.map, arg.idx, arg.access, arg._flatten)
            _arg._loop_position = loop_id
            _arg._position = arg._position
            _arg._indirect_position = arg._indirect_position
            _arg._c_local_maps = c_local_maps
            return _arg

        if isinstance(args, (list, tuple)):
            return [convert(arg, gtl_map, loop_id) for arg in args]
        return convert(args, gtl_map, loop_id)

    @staticmethod
    def filter_args(loop_args):
        """Given a sequence of tuples of ``Args``, where each tuple comes from a
        different loop, create a sequence of ``Args`` where there are no duplicates
        and access modes are properly set (for example, an ``Arg`` whose ``Dat``
        appears in two different tuples with access mode ``WRITE`` and ``READ``,
        respectively, will have access mode ``RW`` in the returned sequence of
        ``Args``."""
        filtered_args = OrderedDict()
        for args in loop_args:
            for a in args:
                filtered_args[a.data] = filtered_args.get(a.data, a)
                if a.access != filtered_args[a.data].access:
                    if READ in [a.access, filtered_args[a.data].access]:
                        # If a READ and some sort of write (MIN, MAX, RW, WRITE,
                        # INC), then the access mode becomes RW
                        filtered_args[a.data]._access = RW
                    elif WRITE in [a.access, filtered_args[a.data].access]:
                        # Can't be a READ, so just stick to WRITE regardless of what
                        # the other access mode is
                        filtered_args[a.data]._access = WRITE
                    else:
                        # Neither READ nor WRITE, so access modes are some
                        # combinations of RW, INC, MIN, MAX. For simplicity,
                        # just make it RW.
                        filtered_args[a.data]._access = RW
        return filtered_args

    def c_arg_bindto(self, arg):
        """Assign c_pointer of this Arg to ``arg``."""
        if self.ctype != arg.ctype:
            raise RuntimeError("Cannot bind arguments having mismatching types")
        return "%s* %s = %s" % (self.ctype, self.c_arg_name(), arg.c_arg_name())

    def c_map_name(self, i, j):
        return self._c_local_maps[i][j]

    @property
    def name(self):
        """The generated argument name."""
        return "arg_exec_loop%d_%d" % (self._loop_position, self._position)


class Kernel(host.Kernel, tuple):

    """A :class:`fusion.Kernel` object represents an ordered sequence of kernels.
    The sequence can either be the result of the concatenation of the kernels
    bodies, or a list of separate kernels (i.e., different C functions).
    """

    @classmethod
    def _cache_key(cls, kernels, fused_ast=None, loop_chain_index=None):
        keys = "".join([super(Kernel, cls)._cache_key(k._code or k._ast.gencode(),
                                                      k._name, k._opts, k._include_dirs,
                                                      k._headers, k._user_code)
                        for k in kernels])
        return str(loop_chain_index) + keys

    def _ast_to_c(self, asts, opts):
        """Fuse Abstract Syntax Trees of a collection of kernels and transform
        them into a string of C code."""
        if not isinstance(asts, (ast.FunDecl, ast.Root)):
            asts = ast.Root(asts)
        self._ast = asts
        return super(Kernel, self)._ast_to_c(self._ast, opts)

    def __init__(self, kernels, fused_ast=None, loop_chain_index=None):
        """Initialize a :class:`fusion.Kernel` object.

        :param kernels: an iterator of some :class:`Kernel` objects. The objects
                        can be of class `fusion.Kernel` or of any superclass.
        :param fused_ast: the Abstract Syntax Tree of the fused kernel. If not
                          provided, kernels are simply concatenated.
        :param loop_chain_index: index (i.e., position) of the kernel in a loop
                                 chain. This can be used to differentiate a same
                                 kernel appearing multiple times in a loop chain.
        """
        # Protect against re-initialization when retrieved from cache
        if self._initialized:
            return
        kernels = as_tuple(kernels, (Kernel, host.Kernel, base.Kernel))

        Kernel._globalcount += 1
        self._kernels = kernels
        self._name = "_".join([k.name for k in kernels])
        self._opts = dict(flatten([k._opts.items() for k in kernels]))
        self._applied_blas = any(k._applied_blas for k in kernels)
        self._include_dirs = list(set(flatten([k._include_dirs for k in kernels])))
        self._headers = list(set(flatten([k._headers for k in kernels])))
        self._user_code = "\n".join(list(set([k._user_code for k in kernels])))

        asts = fused_ast
        if not asts:
            # If kernels' need be concatenated, discard duplicates
            kernels = dict(zip([k.cache_key[1:] for k in kernels], kernels)).values()
            asts = [k._ast for k in kernels]

        # Code generation is delayed until actually needed
        self._ast = asts
        self._code = None

        self._initialized = True

    def __iter__(self):
        for k in self._kernels:
            yield k

    def __str__(self):
        return "OP2 FusionKernel: %s" % self._name


# Parallel loop API

class JITModule(host.JITModule):

    _cppargs = []
    _libraries = []
    _extension = 'cpp'

    _wrapper = """
extern "C" void %(wrapper_name)s(%(executor_arg)s,
                      %(ssinds_arg)s
                      %(wrapper_args)s
                      %(const_args)s);
void %(wrapper_name)s(%(executor_arg)s,
                      %(ssinds_arg)s
                      %(wrapper_args)s
                      %(const_args)s) {
  %(user_code)s
  %(wrapper_decs)s;
  %(const_inits)s;

  %(executor_code)s;
}
"""
    _kernel_wrapper = """
%(interm_globals_decl)s;
%(interm_globals_init)s;
%(vec_decs)s;
%(args_binding)s;
%(tile_init)s;
for (int n = %(tile_start)s; n < %(tile_end)s; n++) {
  int i = %(index_expr)s;
  %(vec_inits)s;
  i = %(tile_iter)s[%(index_expr)s];
  %(buffer_decl)s;
  %(buffer_gather)s
  %(kernel_name)s(%(kernel_args)s);
  %(layout_decl)s;
  %(layout_loop)s
      %(layout_assign)s;
  %(layout_loop_close)s
  i = %(index_expr)s;
  %(itset_loop_body)s;
}
%(interm_globals_writeback)s;
"""

    @classmethod
    def _cache_key(cls, kernel, it_space, *args, **kwargs):
        key = (hash(kwargs['executor']),)
        all_args = kwargs['all_args']
        for kernel_i, it_space_i, args_i in zip(kernel, it_space, all_args):
            key += super(JITModule, cls)._cache_key(kernel_i, it_space_i, *args_i)
        return key

    def __init__(self, kernel, it_space, *args, **kwargs):
        if self._initialized:
            return
        self._all_args = kwargs.pop('all_args')
        self._executor = kwargs.pop('executor')
        super(JITModule, self).__init__(kernel, it_space, *args, **kwargs)

    def compile(self, argtypes=None, restype=None):
        if hasattr(self, '_fun'):
            # It should not be possible to pull a jit module out of
            # the cache /with/ arguments
            if hasattr(self, '_args'):
                raise RuntimeError("JITModule is holding onto args, memory leak!")
            self._fun.argtypes = argtypes
            self._fun.restype = restype
            return self._fun
        # If we weren't in the cache we /must/ have arguments
        if not hasattr(self, '_args'):
            raise RuntimeError("JITModule not in cache, but has no args associated")

        # Prior to the instantiation and compilation of the JITModule, a fusion
        # kernel object needs be created. This is because the superclass' method
        # expects a single kernel, not a list as we have at this point.
        self._kernel = Kernel(self._kernel)
        # Set compiler and linker options
        slope_dir = os.environ['SLOPE_DIR']
        self._kernel._name = 'executor'
        self._kernel._headers.extend(slope.Executor.meta['headers'])
        self._kernel._include_dirs.extend(['%s/%s' % (slope_dir,
                                                      slope.get_include_dir())])
        self._libraries += ['-L%s/%s' % (slope_dir, slope.get_lib_dir()),
                            '-l%s' % slope.get_lib_name()]
        compiler = coffee.plan.compiler.get('name')
        self._cppargs += slope.get_compile_opts(compiler)
        fun = super(JITModule, self).compile(argtypes, restype)

        if hasattr(self, '_all_args'):
            # After the JITModule is compiled, can drop any reference to now
            # useless fields, which would otherwise cause memory leaks
            del self._all_args
            del self._executor

        return fun

    def generate_code(self):
        indent = lambda t, i: ('\n' + '  ' * i).join(t.split('\n'))
        code_dict = {}

        code_dict['wrapper_name'] = 'wrap_executor'
        code_dict['executor_arg'] = "%s %s" % (slope.Executor.meta['ctype_exec'],
                                               slope.Executor.meta['name_param_exec'])

        # Construct the wrapper
        _wrapper_args = ', '.join([arg.c_wrapper_arg() for arg in self._args])
        _wrapper_decs = ';\n'.join([arg.c_wrapper_dec() for arg in self._args])
        if len(Const._defs) > 0:
            _const_args = ', '
            _const_args += ', '.join([c_const_arg(c) for c in Const._definitions()])
        else:
            _const_args = ''
        _const_inits = ';\n'.join([c_const_init(c) for c in Const._definitions()])

        code_dict['wrapper_args'] = _wrapper_args
        code_dict['const_args'] = _const_args
        code_dict['wrapper_decs'] = indent(_wrapper_decs, 1)
        code_dict['const_inits'] = indent(_const_inits, 1)

        # Construct kernels invocation
        _loop_chain_body, _user_code, _ssinds_arg = [], [], []
        for i, loop in enumerate(zip(self._kernel, self._itspace, self._all_args)):
            kernel, it_space, args = loop

            # Obtain code_dicts of individual kernels, since these have pieces of
            # code that can be straightforwardly reused for this code generation
            loop_code_dict = host.JITModule(kernel, it_space, *args).generate_code()

            # Need to bind executor arguments to this kernel's arguments
            # Using a dict because need comparison on identity, not equality
            args_dict = dict(zip([_a.data for _a in self._args], self._args))
            binding = OrderedDict(zip(args, [args_dict[a.data] for a in args]))
            if len(binding) != len(args):
                raise RuntimeError("Tiling code gen failed due to args mismatching")
            binding = ';\n'.join([a0.c_arg_bindto(a1) for a0, a1 in binding.items()])

            loop_code_dict['args_binding'] = binding
            loop_code_dict['tile_iter'] = self._executor.gtl_maps[i]['DIRECT']
            loop_code_dict['tile_init'] = self._executor.c_loop_init[i]
            loop_code_dict['tile_start'] = slope.Executor.meta['tile_start']
            loop_code_dict['tile_end'] = slope.Executor.meta['tile_end']

            _loop_chain_body.append(strip(JITModule._kernel_wrapper % loop_code_dict))
            _user_code.append(kernel._user_code)
            _ssinds_arg.append(loop_code_dict['ssinds_arg'])
        _loop_chain_body = "\n\n".join(_loop_chain_body)
        _user_code = "\n".join(_user_code)
        _ssinds_arg = ", ".join([s for s in _ssinds_arg if s])

        code_dict['user_code'] = indent(_user_code, 1)
        code_dict['ssinds_arg'] = _ssinds_arg
        executor_code = indent(self._executor.c_code(indent(_loop_chain_body, 2)), 1)
        code_dict['executor_code'] = executor_code

        return code_dict


class ParLoop(host.ParLoop):

    def __init__(self, kernel, it_space, *args, **kwargs):
        read_args = [a.data for a in args if a.access in [READ, RW]]
        written_args = [a.data for a in args if a.access in [RW, WRITE, MIN, MAX, INC]]
        inc_args = [a.data for a in args if a.access in [INC]]
        LazyComputation.__init__(self, set(read_args) | Const._defs,
                                 set(written_args), set(inc_args))

        self._kernel = kernel
        self._actual_args = args
        self._it_space = it_space

        for i, arg in enumerate(self._actual_args):
            arg.position = i
            arg.indirect_position = i
        for i, arg1 in enumerate(self._actual_args):
            if arg1._is_dat and arg1._is_indirect:
                for arg2 in self._actual_args[i:]:
                    # We have to check for identity here (we really
                    # want these to be the same thing, not just look
                    # the same)
                    if arg2.data is arg1.data and arg2.map is arg1.map:
                        arg2.indirect_position = arg1.indirect_position

        # These parameters are expected in a tiled ParLoop
        self._all_args = kwargs.get('all_args', [args])
        self._inspection = kwargs.get('inspection')
        self._executor = kwargs.get('executor')

    @collective
    @profile
    def compute(self):
        """Execute the kernel over all members of the iteration space."""
        with timed_region("ParLoopChain: compute"):
            self._compute()

    @collective
    @lineprof
    def _compute(self):
        kwargs = {
            'all_args': self._all_args,
            'executor': self._executor,
        }
        fun = JITModule(self.kernel, self.it_space, *self.args, **kwargs)

        # Build restype, argtypes and argvalues
        self._restype = None
        self._argtypes = [slope.Executor.meta['py_ctype_exec']]
        self._jit_args = [self._inspection]
        for it_space in self.it_space:
            if isinstance(it_space._iterset, Subset):
                self._argtypes.append(it_space._iterset._argtype)
                self._jit_args.append(it_space._iterset._indices)
        for arg in self.args:
            if arg._is_mat:
                self._argtypes.append(arg.data._argtype)
                self._jit_args.append(arg.data.handle.handle)
            else:
                for d in arg.data:
                    # Cannot access a property of the Dat or we will force
                    # evaluation of the trace
                    self._argtypes.append(d._argtype)
                    self._jit_args.append(d._data)

            if arg._is_indirect or arg._is_mat:
                maps = as_tuple(arg.map, Map)
                for map in maps:
                    for m in map:
                        self._argtypes.append(m._argtype)
                        self._jit_args.append(m.values_with_halo)

        for c in Const._definitions():
            self._argtypes.append(c._argtype)
            self._jit_args.append(c.data)

        # Compile and run the JITModule
        fun = fun.compile(argtypes=self._argtypes, restype=self._restype)
        with timed_region("ParLoopChain: executor"):
            fun(*self._jit_args)


# Possible Schedules as produced by an Inspector

class Schedule(object):
    """Represent an execution scheme for a sequence of :class:`ParLoop` objects."""

    def __init__(self, kernel):
        self._kernel = list(kernel)

    def __call__(self, loop_chain):
        """The argument ``loop_chain`` is a list of :class:`ParLoop` objects,
        which is expected to be mapped onto an optimized scheduling.

        In the simplest case, this Schedule's kernels exactly match the :class:`Kernel`
        objects in ``loop_chain``; the default PyOP2 execution model should then be
        used, and an unmodified ``loop_chain`` therefore be returned.

        In other scenarios, this Schedule's kernels could represent the fused
        version, or the tiled version, of the provided ``loop_chain``; a sequence
        of new :class:`ParLoop` objects using the fused/tiled kernels should be
        returned.
        """
        raise NotImplementedError("Subclass must implement ``__call__`` method")


class PlainSchedule(Schedule):

    def __init__(self):
        super(PlainSchedule, self).__init__([])

    def __call__(self, loop_chain):
        return loop_chain


class FusionSchedule(Schedule):
    """Schedule for a sequence of soft/hard fused :class:`ParLoop` objects."""

    def __init__(self, kernels, offsets):
        super(FusionSchedule, self).__init__(kernels)
        # Track the indices of the loop chain's /ParLoop/s each fused kernel maps to
        offsets = [0] + list(offsets)
        loops_indices = [range(offsets[i], o) for i, o in enumerate(offsets[1:])]
        self._info = [{'loops_indices': li} for li in loops_indices]

    def __call__(self, loop_chain):
        fused_par_loops = []
        for kernel, info in zip(self._kernel, self._info):
            loops_indices = info['loops_indices']
            extra_args = info.get('extra_args', [])

            # Create the ParLoop's arguments. Note that both the iteration set and
            # the iteration region must be the same for all loops being fused
            iterregion = loop_chain[loops_indices[0]].iteration_region
            iterset = loop_chain[loops_indices[0]].it_space.iterset
            loops = [loop_chain[i] for i in loops_indices]
            args = Arg.filter_args([loop.args for loop in loops]).values() + extra_args

            # Create the actual ParLoop, resulting from the fusion of some kernels
            fused_par_loops.append(_make_object('ParLoop', kernel, iterset, *args,
                                                **{'iterate': iterregion}))
        return fused_par_loops

    def _hard_fuse(self, fused):
        """Update the schedule by introducing kernels produced by hard fusion."""
        for fused_kernel, fused_map in fused:
            base, fuse = fused_kernel._kernels
            # Variable names: "base" represents the kernel within which "fuse" will
            # be fused into

            # In addition to the union of the "base" and "fuse"' sets of arguments,
            # need to be passed in:
            # - a bitmap, the i-th bit indicating whether the i-th iteration in "fuse"
            #   has been executed
            # - a map from "base"'s iteration space to "fuse"'s iteration space
            # - the arity of such map
            arg_is_executed = Dat(fused_map.toset)(RW, fused_map)
            arg_fused_map = Dat(DataSet(fused_map.iterset, fused_map.arity),
                                fused_map.values)(READ)
            arg_arity = Global(1, fused_map.arity, np.int, "fusion_map_arity")(READ)

            # Update the schedule
            base_idx, fuse_idx = self._kernel.index(base), self._kernel.index(fuse)
            pos = min(base_idx, fuse_idx)
            self._kernel.insert(pos, fused_kernel)
            self._info[pos]['loops_indices'] = [base_idx, fuse_idx]
            # Note: the order is importat: first /arg_is_excuted/ is expected, and
            # then /arg_fused_map/, and finally /arg_arity/
            self._info[pos]['extra_args'] = [arg_is_executed, arg_fused_map, arg_arity]
            self._kernel.pop(pos+1)
            pos = max(base_idx, fuse_idx)
            self._info.pop(pos)
            self._kernel.pop(pos)


class TilingSchedule(Schedule):
    """Schedule for a sequence of tiled :class:`ParLoop` objects."""

    def __init__(self, schedule, inspection, executor):
        self._schedule = schedule
        self._inspection = inspection
        self._executor = executor

    def __call__(self, loop_chain):
        loop_chain = self._schedule(loop_chain)
        args = Arg.filter_args([loop.args for loop in loop_chain]).values()
        kernel = tuple((loop.kernel for loop in loop_chain))
        all_args = tuple((Arg.specialize(loop.args, gtl_map, i) for i, (loop, gtl_map)
                         in enumerate(zip(loop_chain, self._executor.gtl_maps))))
        it_space = tuple((loop.it_space for loop in loop_chain))
        kwargs = {
            'inspection': self._inspection,
            'all_args': all_args,
            'executor': self._executor
        }
        return [ParLoop(kernel, it_space, *args, **kwargs)]


# Loop chain inspection

class Inspector(Cached):
    """An inspector is used to fuse or tile a sequence of :class:`ParLoop` objects.

    For tiling, the inspector exploits the SLOPE library, which the user makes
    visible by setting the environment variable ``SLOPE_DIR`` to the root SLOPE
    directory."""

    _cache = {}
    _modes = ['soft', 'hard', 'tile']

    @classmethod
    def _cache_key(cls, name, loop_chain, tile_size):
        key = (name, tile_size)
        for loop in loop_chain:
            if isinstance(loop, Mat._Assembly):
                continue
            key += (hash(str(loop.kernel._ast)),)
            for arg in loop.args:
                if arg._is_global:
                    key += (arg.data.dim, arg.data.dtype, arg.access)
                elif arg._is_dat:
                    if isinstance(arg.idx, IterationIndex):
                        idx = (arg.idx.__class__, arg.idx.index)
                    else:
                        idx = arg.idx
                    map_arity = arg.map.arity if arg.map else None
                    key += (arg.data.dim, arg.data.dtype, map_arity, idx, arg.access)
                elif arg._is_mat:
                    idxs = (arg.idx[0].__class__, arg.idx[0].index,
                            arg.idx[1].index)
                    map_arities = (arg.map[0].arity, arg.map[1].arity)
                    key += (arg.data.dims, arg.data.dtype, idxs, map_arities, arg.access)
        return key

    def __init__(self, name, loop_chain, tile_size):
        if self._initialized:
            return
        if not hasattr(self, '_inspected'):
            # Initialization can occur more than once (until the inspection is
            # actually performed), but only the first time this attribute is set
            self._inspected = 0
        self._name = name
        self._tile_size = tile_size
        self._loop_chain = loop_chain

    def inspect(self, mode):
        """Inspect this Inspector's loop chain and produce a Schedule object.

        :param mode: can take any of the values in ``Inspector._modes``, namely
                     ``soft``, ``hard``, and ``tile``. If ``soft`` is specified,
                     only soft fusion takes place; that is, only consecutive loops
                     over the same iteration set that do not present RAW or WAR
                     dependencies through indirections are fused. If ``hard`` is
                     specified, then first ``soft`` is applied, followed by fusion
                     of loops over different iteration sets, provided that RAW or
                     WAR dependencies are not present. If ``tile`` is specified,
                     than tiling through the SLOPE library takes place just after
                     ``soft`` and ``hard`` fusion.
        """
        self._inspected += 1
        if self._heuristic_skip_inspection(mode):
            # Heuristically skip this inspection if there is a suspicion the
            # overhead is going to be too much; for example, when the loop
            # chain could potentially be execution only once or a few time.
            # Blow away everything we don't need any more
            del self._name
            del self._loop_chain
            del self._tile_size
            return PlainSchedule()
        elif hasattr(self, '_schedule'):
            # An inspection plan is in cache.
            # It should not be possible to pull a jit module out of the cache
            # /with/ the loop chain
            if hasattr(self, '_loop_chain'):
                raise RuntimeError("Inspector is holding onto loop_chain, memory leaks!")
            # The fusion mode was recorded, and must match the one provided for
            # this inspection
            if self.mode != mode:
                raise RuntimeError("Cached Inspector's mode doesn't match")
            return self._schedule
        elif not hasattr(self, '_loop_chain'):
            # The inspection should be executed /now/. We weren't in the cache,
            # so we /must/ have a loop chain
            raise RuntimeError("Inspector must have a loop chain associated with it")
        # Finally, we check the legality of `mode`
        if mode not in Inspector._modes:
            raise TypeError("Inspection accepts only %s fusion modes",
                            str(Inspector._modes))
        self._mode = mode
        mode = Inspector._modes.index(mode)

        with timed_region("ParLoopChain `%s`: inspector" % self._name):
            self._soft_fuse()
            if mode > 0:
                self._hard_fuse()
            if mode > 1:
                self._tile()

        # A schedule has been computed by any of /_soft_fuse/, /_hard_fuse/ or
        # or /_tile/; therefore, consider this Inspector initialized, and
        # retrievable from cache in subsequent calls to inspect().
        self._initialized = True

        # Blow away everything we don't need any more
        del self._name
        del self._loop_chain
        del self._tile_size
        return self._schedule

    def _heuristic_skip_inspection(self, mode):
        """Decide heuristically whether to run an inspection or not."""
        # At the moment, a simple heuristic is used. If tiling is not requested,
        # then inspection and fusion are always performed. If tiling is on the other
        # hand requested, then fusion is performed only if inspection is requested
        # more than once. This is to amortize the cost of inspection due to tiling.
        if mode == 'tile' and self._inspected < 2:
            return True
        return False

    def _filter_kernel_args(self, loops, fundecl):
        """Eliminate redundant arguments in the fused kernel's signature."""
        fused_loop_args = list(flatten([l.args for l in loops]))
        unique_fused_loop_args = Arg.filter_args([l.args for l in loops])
        fused_kernel_args = fundecl.args
        binding = OrderedDict(zip(fused_loop_args, fused_kernel_args))
        new_fused_kernel_args, args_maps = [], []
        for fused_loop_arg, fused_kernel_arg in binding.items():
            unique_fused_loop_arg = unique_fused_loop_args[fused_loop_arg.data]
            if fused_loop_arg is unique_fused_loop_arg:
                new_fused_kernel_args.append(fused_kernel_arg)
                continue
            tobind_fused_kernel_arg = binding[unique_fused_loop_arg]
            if tobind_fused_kernel_arg.is_const:
                # Need to remove the /const/ qualifier from the C declaration
                # if the same argument is written to, somewhere, in the fused
                # kernel. Otherwise, /const/ must be appended, if not present
                # already, to the alias' qualifiers
                if fused_loop_arg._is_written:
                    tobind_fused_kernel_arg.qual.remove('const')
                elif 'const' not in fused_kernel_arg.qual:
                    fused_kernel_arg.qual.append('const')
            # Update the /binding/, since might be useful for the caller
            binding[fused_loop_arg] = tobind_fused_kernel_arg
            # Aliases may be created instead of changing symbol names
            if fused_kernel_arg.sym.symbol == tobind_fused_kernel_arg.sym.symbol:
                continue
            alias = ast_make_alias(dcopy(fused_kernel_arg),
                                   dcopy(tobind_fused_kernel_arg))
            args_maps.append(alias)
        fundecl.children[0].children = args_maps + fundecl.children[0].children
        fundecl.args = new_fused_kernel_args
        return binding

    def _soft_fuse(self):
        """Fuse consecutive loops over the same iteration set by concatenating
        kernel bodies and creating new :class:`ParLoop` objects representing
        the fused sequence.

        The conditions under which two loops over the same iteration set are
        hardly fused are:

            * They are both direct, OR
            * One is direct and the other indirect

        This is detailed in the paper::

            "Mesh Independent Loop Fusion for Unstructured Mesh Applications"

        from C. Bertolli et al.
        """

        def fuse(self, loops, loop_chain_index):
            # Naming convention: here, we are fusing ASTs in /fuse_asts/ within
            # /base_ast/. Same convention will be used in the /hard_fuse/ method
            kernels = [l.kernel for l in loops]
            fuse_asts = [k._ast for k in kernels]
            # Fuse the actual kernels' bodies
            base_ast = dcopy(fuse_asts[0])
            ast_info = ast_visit(base_ast, search=ast.FunDecl)
            base_ast_fundecl = ast_info['search'][ast.FunDecl]
            if len(base_ast_fundecl) != 1:
                raise RuntimeError("Fusing kernels, but found unexpected AST")
            base_ast_fundecl = base_ast_fundecl[0]
            for unique_id, _fuse_ast in enumerate(fuse_asts[1:], 1):
                fuse_ast = dcopy(_fuse_ast)
                # 1) Extend function name
                base_ast_fundecl.name = "%s_%s" % (base_ast.name, fuse_ast.name)
                # 2) Concatenate the arguments in the signature
                base_ast_fundecl.args.extend(fuse_ast.args)
                # 3) Uniquify symbols identifiers
                fuse_ast_info = ast_visit(fuse_ast)
                fuse_ast_decls = fuse_ast_info['decls']
                fuse_ast_symbols = fuse_ast_info['symbols']
                for str_sym, decl in fuse_ast_decls.items():
                    for symbol in fuse_ast_symbols.keys():
                        ast_update_id(symbol, str_sym, unique_id)
                # 4) Concatenate bodies
                marker = [ast.FlatBlock("\n\n// Begin of fused kernel\n\n")]
                base_ast_fundecl.children[0].children.extend(marker + fuse_ast.children)
            # Eliminate redundancies in the fused kernel's signature
            self._filter_kernel_args(loops, base_ast_fundecl)
            # Naming convention
            fused_ast = base_ast
            return Kernel(kernels, fused_ast, loop_chain_index)

        fused, fusing = [], [self._loop_chain[0]]
        for i, loop in enumerate(self._loop_chain[1:]):
            base_loop = fusing[-1]
            if base_loop.it_space != loop.it_space or \
                    (base_loop.is_indirect and loop.is_indirect):
                # Fusion not legal
                fused.append((fuse(self, fusing, len(fused)), i+1))
                fusing = [loop]
            elif (base_loop.is_direct and loop.is_direct) or \
                    (base_loop.is_direct and loop.is_indirect) or \
                    (base_loop.is_indirect and loop.is_direct):
                # This loop is fusible. Also, can speculative go on searching
                # for other loops to fuse
                fusing.append(loop)
            else:
                raise RuntimeError("Unexpected loop chain structure while fusing")
        if fusing:
            fused.append((fuse(self, fusing, len(fused)), len(self._loop_chain)))

        fused_kernels, offsets = zip(*fused)
        self._schedule = FusionSchedule(fused_kernels, offsets)
        self._loop_chain = self._schedule(self._loop_chain)

    def _hard_fuse(self):
        """Fuse consecutive loops over different iteration sets that do not
        present RAW, WAR or WAW dependencies. For examples, two loops like: ::

            par_loop(kernel_1, it_space_1,
                     dat_1_1(INC, ...),
                     dat_1_2(READ, ...),
                     ...)

            par_loop(kernel_2, it_space_2,
                     dat_2_1(INC, ...),
                     dat_2_2(READ, ...),
                     ...)

        where ``dat_1_1 == dat_2_1`` and, possibly (but not necessarily),
        ``it_space_1 != it_space_2``, can be hardly fused. Note, in fact, that
        the presence of ``INC`` does not imply a real WAR dependency, because
        increments are associative."""

        def has_raw_or_war(loop1, loop2):
            # Note that INC after WRITE is a special case of RAW dependency since
            # INC cannot take place before WRITE.
            return loop2.reads & loop1.writes or loop2.writes & loop1.reads or \
                loop1.incs & (loop2.writes - loop2.incs) or \
                loop2.incs & (loop1.writes - loop1.incs)

        def has_iai(loop1, loop2):
            return loop1.incs & loop2.incs

        def fuse(base_loop, loop_chain, fused):
            """Try to fuse one of the loops in ``loop_chain`` with ``base_loop``."""
            for loop in loop_chain:
                if has_raw_or_war(loop, base_loop):
                    # Can't fuse across loops preseting RAW or WAR dependencies
                    return
                if loop.it_space == base_loop.it_space:
                    warning("Ignoring unexpected sequence of loops in loop fusion")
                    continue
                # Is there an overlap in any incremented regions? If that is
                # the case, then fusion can really be useful, by allowing to
                # save on the number of indirect increments or matrix insertions
                common_inc_data = has_iai(base_loop, loop)
                if not common_inc_data:
                    continue
                common_incs = [a for a in base_loop.args + loop.args
                               if a.data in common_inc_data]
                # Hard fusion potentially doable provided that we own a map between
                # the iteration spaces involved
                maps = list(set(flatten([a.map for a in common_incs])))
                maps += [m.factors for m in maps if hasattr(m, 'factors')]
                maps = list(flatten(maps))
                set1, set2 = base_loop.it_space.iterset, loop.it_space.iterset
                fused_map = [m for m in maps if set1 == m.iterset and set2 == m.toset]
                if fused_map:
                    fused.append((base_loop, loop, fused_map[0], common_incs[1]))
                    return
                fused_map = [m for m in maps if set1 == m.toset and set2 == m.iterset]
                if fused_map:
                    fused.append((loop, base_loop, fused_map[0], common_incs[0]))
                    return

        # First, find fusible kernels
        fused = []
        for i, l in enumerate(self._loop_chain, 1):
            fuse(l, self._loop_chain[i:], fused)
        if not fused:
            return

        # Then, create a suitable hard-fusion kernel
        # The hardly-fused kernel will have the following structure:
        #
        # wrapper (args: Union(kernel1, kernel2, extra):
        #   staging of pointers
        #   ...
        #   fusion (staged pointers, ..., extra)
        #   insertion (...)
        #
        # Where /extra/ represents additional arguments, like the map from
        # kernel1's iteration space to kernel2's iteration space. The /fusion/
        # function looks like:
        #
        # fusion (...):
        #   kernel1 (buffer, ...)
        #   for i = 0 to arity:
        #     if not already_executed[i]:
        #       kernel2 (buffer[..], ...)
        #
        # Where /arity/ is the number of kernel2's iterations incident to
        # kernel1's iterations.
        _fused = []
        for base_loop, fuse_loop, fused_map, fused_arg in fused:
            # Start analyzing the kernels' ASTs
            base, fuse = base_loop.kernel, fuse_loop.kernel
            base_info = ast_visit(base._ast, search=(ast.FunDecl, ast.PreprocessNode))
            base_header = base_info['search'][ast.PreprocessNode]
            base_fundecl = base_info['search'][ast.FunDecl]
            if len(base_fundecl) != 1:
                raise RuntimeError("Fusing kernels, but found unexpected AST")
            fuse_info = ast_visit(fuse._ast, search=(ast.FunDecl, ast.PreprocessNode))
            fuse_header = fuse_info['search'][ast.PreprocessNode]
            fuse_fundecl = fuse_info['search'][ast.FunDecl]
            fuse_symbol_refs = fuse_info['symbol_refs']
            if len(base_fundecl) != 1 or len(fuse_fundecl) != 1:
                raise RuntimeError("Fusing kernels, but found unexpected AST")
            base_fundecl = base_fundecl[0]
            fuse_fundecl = fuse_fundecl[0]
            from IPython import embed; embed()

            # Craft the /fusion/ kernel
            # 1) Create /fusion/ arguments and signature
            body = ast.Block([])
            fusion_args = base_fundecl.args + fuse_fundecl.args
            fusion_fundecl = ast.FunDecl(base_fundecl.ret, 'fusion', fusion_args, body)

            # 2) Filter out duplicate arguments, and append extra arguments to
            # the function declaration
            binding = self._filter_kernel_args([base_loop, fuse_loop], fusion_fundecl)
            fusion_fundecl.args += \
                [ast.Decl('int*', ast.Symbol('executed'))] + \
                [ast.Decl('int*', ast.Symbol('fusion_map'))] + \
                [ast.Decl('int', ast.Symbol('fusion_map_arity'))]

            # 3) Create /fusion/ body
            base_funcall_syms = [ast.Symbol(d.sym.symbol)
                                 for d in base_fundecl.args]
            base_funcall = ast.FunCall(base_fundecl.name, *base_funcall_syms)
            fuse_funcall_syms = [ast.Symbol(binding[arg].sym.symbol)
                                 for arg in fuse_loop.args]
            fuse_funcall = ast.FunCall(fuse_fundecl.name, *fuse_funcall_syms)
            ind_iter_idx = ast.Decl('int', ast.Symbol('fused_iter'),
                                    ast.Symbol('fusion_map', ('i')))
            if_cond = ast.Not(ast.Symbol('executed', ('fused_iter',)))
            if_update = ast.Assign(ast.Symbol('executed', ('fused_iter',)),
                                   ast.Symbol('1'))
            if_exec = ast.If(if_cond, [ast.Block([fuse_funcall,
                                                  if_update], open_scope=True)])
            fuse_body = ast.Block([ind_iter_idx, if_exec], open_scope=True)
            fuse_for = ast.c_for('i', 'fusion_map_arity', fuse_body, pragma="")
            body.children.extend([base_funcall, fuse_for.children[0]])

            # Modify /fuse/ kernel to accomodate fused increments
            # 1) Determine /fuse/'s incremented argument
            fuse_symbol_refs = ast_visit(fuse_fundecl)['symbol_refs']
            fuse_inc_decl = binding[fused_arg]
            fuse_inc_refs = fuse_symbol_refs[fuse_inc_decl.sym.symbol]
            fuse_inc_refs = [sym for sym, parent in fuse_inc_refs
                             if not isinstance(parent, ast.Decl)]

            # 2) Create and introduce offsets for accumulating increments
            # Note: the /fused_map/ is a factor of the base_loop's iteration set map,
            # so the order the /fuse/ loop's iterations are executed (in the /for i=0
            # to arity/ loop) reflects the order of the entries in /fused_map/
            ofs_syms, ofs_decls = [], []
            for b in fused_arg._block_shape:
                for rc in b:
                    # Determine offset values and produce corresponding C symbols
                    _ofs_vals = [[0] for i in range(len(rc))]
                    for i, ofs in enumerate(rc):
                        ofs_syms.append(ast.Symbol('ofs%d' % i))
                        ofs_decls.append(ast.Decl('int', dcopy(ofs_syms[i])))
                        _ofs_vals[i].append(ofs)
                    for s in fuse_inc_refs:
                        s.offset = tuple((1, o) for o in ofs_syms)
                    # Add offset array to the /fusion/ kernel body
                    ofs_vals = '{%s}' % ','.join(['{%s}' % ','.join([str(i) for i in v])
                                                  for v in _ofs_vals])
                    ofs_array = ast.Symbol('ofs', (len(_ofs_vals), len(_ofs_vals[0])))
                    ofs_array = ast.Decl('int', ofs_array, ast.ArrayInit(ofs_vals),
                                         qualifiers=['static', 'const'])
                    body.children.insert(0, ofs_array)
                    # Set offset value and append it to the If's Then block
                    ofs_assign = [ast.Decl('int', dcopy(s), ast.Symbol('ofs', (i, 'i')))
                                  for i, s in enumerate(ofs_syms)]
                    if_exec.children[0].children[:0] = ofs_assign

            # 3) Change /fuse/ kernel invocation and function declaration
            fuse_funcall.children.extend(ofs_syms)
            fuse_fundecl.args.extend(ofs_decls)

            # 4) Create a /fusion.Kernel/ object to be used to update the schedule
            fused_ast = ast.Root([base_fundecl, fuse_fundecl, fusion_fundecl])
            _fused.append((Kernel([base, fuse], fused_ast), fused_map))

        # Finally, generate a new schedule
        self._schedule._hard_fuse(_fused)
        self._loop_chain = self._schedule(self._loop_chain)

    def _tile(self):
        """Tile consecutive loops over different iteration sets characterized
        by RAW and WAR dependencies. This requires interfacing with the SLOPE
        library."""
        try:
            backend_map = {'sequential': 'SEQUENTIAL', 'openmp': 'OMP'}
            slope_backend = backend_map[configuration['backend']]
            slope.set_exec_mode(slope_backend)
            log_info("SLOPE backend set to %s" % slope_backend)
        except KeyError:
            warning("Unable to set backend %s for SLOPE" % configuration['backend'])

        inspector = slope.Inspector()

        # Build arguments types and values
        arguments = []
        insp_sets, insp_maps, insp_loops = set(), {}, []
        for loop in self._loop_chain:
            slope_desc = set()
            # Add sets
            insp_sets.add((loop.it_space.name, loop.it_space.core_size))
            for a in loop.args:
                maps = as_tuple(a.map, Map)
                # Add maps (there can be more than one per argument if the arg
                # is actually a Mat - in which case there are two maps - or if
                # a MixedMap) and relative descriptors
                if not maps:
                    slope_desc.add(('DIRECT', a.access._mode))
                    continue
                for i, map in enumerate(maps):
                    for j, m in enumerate(map):
                        map_name = "%s%d_%d" % (m.name, i, j)
                        insp_maps[m.name] = (map_name, m.iterset.name,
                                             m.toset.name, m.values)
                        slope_desc.add((map_name, a.access._mode))
            # Add loop
            insp_loops.append((loop.kernel.name, loop.it_space.name, list(slope_desc)))
        # Provide structure of loop chain to the SLOPE's inspector
        arguments.extend([inspector.add_sets(insp_sets)])
        arguments.extend([inspector.add_maps(insp_maps.values())])
        inspector.add_loops(insp_loops)
        # Get type and value of any additional arguments that the SLOPE's inspector
        # expects
        arguments.extend([inspector.set_external_dats()])

        # Set a specific tile size
        arguments.extend([inspector.set_tile_size(self._tile_size)])

        # Arguments types and values
        argtypes, argvalues = zip(*arguments)

        # Generate inspector C code
        src = inspector.generate_code()

        # Return type of the inspector
        rettype = slope.Executor.meta['py_ctype_exec']

        # Compiler and linker options
        slope_dir = os.environ['SLOPE_DIR']
        compiler = coffee.plan.compiler.get('name')
        cppargs = slope.get_compile_opts(compiler)
        cppargs += ['-I%s/%s' % (slope_dir, slope.get_include_dir())]
        ldargs = ['-L%s/%s' % (slope_dir, slope.get_lib_dir()),
                  '-l%s' % slope.get_lib_name()]

        # Compile and run inspector
        fun = compilation.load(src, "cpp", "inspector", cppargs, ldargs,
                               argtypes, rettype, compiler)
        inspection = fun(*argvalues)

        # Finally, get the Executor representation, to be used at executor's
        # code generation time
        executor = slope.Executor(inspector)

        self._schedule = TilingSchedule(self._schedule, inspection, executor)

    @property
    def mode(self):
        return self._mode


# Interface for triggering loop fusion

def fuse(name, loop_chain, tile_size):
    """Given a list of :class:`ParLoop` in ``loop_chain``, return a list of new
    :class:`ParLoop` objects implementing an optimized scheduling of the loop chain.

    .. note:: The unmodified loop chain is instead returned if any of these
    conditions verify:

        * the function is invoked on a previoulsy fused ``loop_chain``
        * a global reduction is present;
        * tiling in enabled and at least one loop iterates over an extruded set
    """
    if len(loop_chain) in [0, 1]:
        # Nothing to fuse
        return loop_chain

    # Search for _Assembly objects since they introduce a synchronization point;
    # that is, loops cannot be fused across an _Assembly object. In that case, try
    # to fuse only the segment of loop chain right before the synchronization point
    remainder = []
    synch_points = [l for l in loop_chain if isinstance(l, Mat._Assembly)]
    if synch_points:
        if len(synch_points) > 1:
            warning("Fusing loops and found more than one synchronization point")
        synch_point = loop_chain.index(synch_points[0])
        remainder, loop_chain = loop_chain[synch_point:], loop_chain[:synch_point]

    # If loops in /loop_chain/ are already /fusion/ objects (this could happen
    # when loops had already been fused because in a /loop_chain/ context) or
    # if global reductions are present, return
    if any([isinstance(l, ParLoop) for l in loop_chain]) or \
            any([l._reduced_globals for l in loop_chain]):
        return loop_chain

    # Loop fusion requires modifying kernels, so ASTs must be present
    if any([not l.kernel._ast for l in loop_chain]):
        return loop_chain

    mode = 'hard'
    if tile_size > 0:
        mode = 'tile'
        # Loop tiling is performed through the SLOPE library, which must be
        # accessible by reading the environment variable SLOPE_DIR
        try:
            os.environ['SLOPE_DIR']
        except KeyError:
            warning("Set the env variable SLOPE_DIR to the location of SLOPE")
            warning("Loops won't be fused, and plain ParLoops will be executed")
            return loop_chain

        # If iterating over an extruded set, return (since the feature is not
        # currently supported)
        if any([l.is_layered for l in loop_chain]):
            return loop_chain

    # Get an inspector for fusing this loop_chain, possibly retrieving it from
    # the cache, and obtain the fused ParLoops through the schedule it produces
    inspector = Inspector(name, loop_chain, tile_size)
    schedule = inspector.inspect(mode)
    return schedule(loop_chain) + remainder


@contextmanager
def loop_chain(name, time_unroll=1, tile_size=0):
    """Analyze the sub-trace of loops lazily evaluated in this contextmanager ::

        [loop_0, loop_1, ..., loop_n-1]

    and produce a new sub-trace (``m <= n``) ::

        [fused_loops_0, fused_loops_1, ..., fused_loops_m-1, peel_loops]

    which is eventually inserted in the global trace of :class:`ParLoop` objects.

    That is, sub-sequences of :class:`ParLoop` objects are potentially replaced by
    new :class:`ParLoop` objects representing the fusion or the tiling of the
    original trace slice.

    :param name: identifier of the loop chain
    :param time_unroll: in a time stepping loop, the length of the loop chain
                        is given by ``num_loops * time_unroll``, where ``num_loops``
                        is the number of loops per time loop iteration. Therefore,
                        setting this value to a number greater than 1 enables
                        fusing/tiling longer loop chains (optional, defaults to 1).
    :param tile_size: suggest a tile size in case loop tiling is used (optional).
                      If ``0`` is passed in, only soft fusion is performed.
    """
    from base import _trace
    trace = _trace._trace
    stamp = trace[-1:]

    yield

    if time_unroll < 1:
        return

    start_point = trace.index(stamp[0])+1 if stamp else 0
    extracted_loop_chain = trace[start_point:]

    # Unroll the loop chain ``time_unroll`` times before fusion/tiling
    total_loop_chain = loop_chain.unrolled_loop_chain + extracted_loop_chain
    if len(total_loop_chain) / len(extracted_loop_chain) == time_unroll:
        start_point = trace.index(total_loop_chain[0])
        trace[start_point:] = fuse(name, total_loop_chain, tile_size)
        loop_chain.unrolled_loop_chain = []
    else:
        loop_chain.unrolled_loop_chain.extend(extracted_loop_chain)
loop_chain.unrolled_loop_chain = []
