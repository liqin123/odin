"""Annotated computation graph management."""
from __future__ import print_function, absolute_import, division

import os
import warnings
import numbers
from numbers import Number
from contextlib import contextmanager
from collections import OrderedDict
from itertools import chain

import numpy as np

import theano
from theano import Variable
from theano import tensor as T
from theano.gof import graph
from theano.sandbox.rng_mrg import MRG_RandomStreams
from theano.scan_module.scan_op import Scan
from theano.gof.graph import Constant
from theano.tensor.shared_randomstreams import RandomStateSharedVariable
from theano.tensor.sharedvar import SharedVariable

from odin.basic import (add_role, has_roles,
                        add_shape, get_shape,
                        AUXILIARY, PARAMETER)
from odin.utils.decorators import singleton
from odin.utils import dict_union, as_shape_tuple
from odin.config import CONFIG

FLOATX = CONFIG.floatX
NPROCESSORS = CONFIG['device_info']['n']


# ===========================================================================
# Dummy method to be compatible with tensorflow
# ===========================================================================
def set_session(session):
    warnings.warn('Current backend is "theano", SESSION is only available in tensorflow.')


def get_session():
    warnings.warn('Current backend is "theano", SESSION is only available in tensorflow.')


# ===========================================================================
# Shape helpers
# ===========================================================================
def _unique(seq, key=None):
    """ Copyright (c) 2013 Matthew Rocklin

    Return only unique elements of a sequence

    >>> tuple(unique((1, 2, 3)))
    (1, 2, 3)
    >>> tuple(unique((1, 2, 1, 3)))
    (1, 2, 3)

    Uniqueness can be defined by key keyword

    >>> tuple(unique(['cat', 'mouse', 'dog', 'hen'], key=len))
    ('cat', 'mouse')

    """
    seen = set()
    seen_add = seen.add
    if key is None:
        for item in seq:
            if item not in seen:
                seen_add(item)
                yield item
    else:  # calculate key
        for item in seq:
            val = key(item)
            if val not in seen:
                seen_add(val)
                yield item


def auto_infer_shape(ops, *var, **kwargs):
    """ You can set 'group_inputs' in kwargs so the inputs to ops
    will be ops(var) instead of ops(*var)
    """
    try:
        inputs = []
        for i in var:
            if isinstance(i, numbers.Number):
                inputs.append(i)
            else:
                input_shape = (0 if s is None or (isinstance(s, Number) and s < 0)
                               else s
                               for s in get_shape(i))
                inputs.append(T.alloc(0, *input_shape))
        if 'group_inputs' in kwargs:
            del kwargs['group_inputs']
            output_shape = ops(inputs, **kwargs).shape.eval()
        else:
            output_shape = ops(*inputs, **kwargs).shape.eval()
        return tuple(s if s else None for s in output_shape)
    except theano.gof.MissingInputError:
        return 'None'


# ===========================================================================
# Basic query
# ===========================================================================
def is_placeholder(variable):
    """Check if variable is a user-provided graph input.

    To be considered an input the variable must have no owner, and not
    be a constant or shared variable.

    Parameters
    ----------
    variable : :class:`~tensor.TensorVariable`

    Returns
    -------
    bool
        ``True`` If the variable is a user-provided input to the graph.

    """
    return (not variable.owner and
            not isinstance(variable, SharedVariable) and
            not isinstance(variable, Constant))


def is_trainable_variable(variable):
    """Check if a variable is a Theano shared variable.

    Notes
    -----
    This function excludes shared variables that store the state of Theano
    random number generators.

    """
    return (isinstance(variable, SharedVariable) and
            not isinstance(variable, RandomStateSharedVariable) and
            not hasattr(variable.tag, 'is_rng'))


def is_variable(variable):
    """ a variable is any tensor variable in (e.g. placeholder,
    trainable_variable, intermediate tensor, ...)
    """
    return isinstance(variable, Variable)


# ===========================================================================
# VALUE MANIPULATION
# ===========================================================================
def get_value(x):
    if isinstance(x, (tuple, list)):
        return [i.get_value(borrow=False) for i in x]
    if not hasattr(x, 'get_value'):
        raise Exception("'get_value() can only be called on a variable. " +
                        "If you have an expression instead, use eval().")
    return x.get_value(borrow=False)


def set_value(x, value):
    x.set_value(np.asarray(value, dtype=x.dtype))


# ===========================================================================
# VARIABLE MANIPULATION
# ===========================================================================
_CURRENT_VARIABLE_SCOPE = ""
_CREATED_VARIABLE = {}
# var id start from 0 and increasing to make sure no duplicate variable
_VAR_ID = 0


@contextmanager
def variable_scope(scope):
    global _CURRENT_VARIABLE_SCOPE
    old_scope = _CURRENT_VARIABLE_SCOPE
    _CURRENT_VARIABLE_SCOPE = str(scope)
    yield None
    _CURRENT_VARIABLE_SCOPE = old_scope


def _check_target(target):
    if CONFIG['multigpu']:
        if target is None:
            target = 'dev0'
        elif isinstance(target, numbers.Number):
            target = 'dev%d' % (int(target) % NPROCESSORS)
        else:
            target = str(target)
    else:
        target = None
    return target


def variable(value, dtype=FLOATX, name=None, target=None):
    """Instantiate a tensor variable.
    """
    # ensure unique name
    if name is None:
        global _VAR_ID; name = 'VAR_%d' % _VAR_ID; _VAR_ID += 1
    # ====== get the right scope for variable ====== #
    if len(_CURRENT_VARIABLE_SCOPE) > 0:
        name = _CURRENT_VARIABLE_SCOPE + "/" + name
    # ====== check loaded variable ====== #
    if name in _CREATED_VARIABLE:
        var = _CREATED_VARIABLE[name]
        if get_shape(var) != value.shape:
            raise Exception('Found pre-defined variable with shape="%s" but new'
                            ' value has shape="%s"' % (get_shape(var), value.shape))
        else:
            warnings.warn("Load value of new variable to old variable, "
                          "var's name:" + name)
        var.set_value(value.astype(var.dtype), borrow=False)
        return var
    # ====== validate inputs ====== #
    value = np.asarray(value, dtype=dtype)
    target = _check_target(target)
    kwargs = {}
    if target is not None:
        kwargs['target'] = target
    # something wrong with SharedVariable constructor for numpy boolean array
    if value.dtype == np.bool:
        value = value.astype('uint8')
    variable = theano.shared(value=value, name=name, strict=False, **kwargs)
    add_shape(variable, tuple(variable.shape.eval()))
    # ====== save all created variable ====== #
    _CREATED_VARIABLE[name] = variable # save original shared variables
    return variable


def placeholder(shape, dtype=FLOATX, name=None):
    """Instantiate an input data placeholder variable.
    """
    shape = as_shape_tuple(shape)
    broadcast = tuple([True if i == 1 else False for i in shape])
    # ====== Modify add name prefix ====== #
    placeholder = T.TensorType(dtype, broadcast)(name)
    # store the predefined shape of placeholder
    add_shape(placeholder, shape)
    return placeholder


def as_tensor_variable(x, name=None, dtype=None):
    if dtype is None:
        dtype = x.dtype
    x = T.as_tensor_variable(x, name=name)
    if x.dtype != dtype:
        x = T.cast(x, dtype)
    return x


def constant(value, dtype=None, shape=None, name='Const'):
    x = T.constant(value, dtype=dtype,
                   ndim=None if shape is None else len(shape),
                   name=name)
    add_shape(x, x.shape.eval())
    return x


# ===========================================================================
# ComputationGraph
# ===========================================================================
@singleton
class ComputationGraph(object):
    r"""Encapsulates a managed Theano computation graph.

    This implies that it not only contains the variables required to
    compute the given outputs, but also all the auxiliary variables and
    updates that were attached to these variables through the annotation
    system.

    All variables are presented in topologically sorted order according to
    the apply nodes that they are an input to.

    Parameters
    ----------
    outputs : (list of) :class:`~tensor.TensorVariable`
        The output(s) of the computation graph.

    Attributes
    ----------
    inputs : list of :class:`~tensor.TensorVariable`
        The inputs of the computation graph. This does not include shared
        variables and constants.
    trainable_variables : list of :class:`~tensor.TensorSharedVariable`
        All the shared variables in the graph.
    parameters : list of :class:`~tensor.TensorSharedVariable`
        All the shared variables which have the :const:`.PARAMETER` role.
    outputs : list of :class:`~tensor.TensorVariable`
        The outputs of the computations graph (as passed to the
        constructor).
    auxiliary_variables : list of :class:`~tensor.TensorVariable`
        All variables which have the :const:`.AUXILIARY` role.
    intermediary_variables : list of :class:`~tensor.TensorVariable`
        Any variable that is not part of :attr:`inputs` or :attr:`outputs`.
    variables : list of :class:`~tensor.TensorVariable`
        All variables (including auxiliary) in the managed graph.
    scans : list of :class:`~theano.scan_module.scan_op.Scan`
        All Scan ops used in this computation graph.
    scan_variables : list of :class:`~tensor.TensorVariable`
        All variables of the inner graphs of Scan ops.
    updates : :class:`~tensor.TensorSharedVariable` updates
        All the updates found attached to the annotations.

    """

    def __init__(self, outputs):
        if not isinstance(outputs, (tuple, list)):
            outputs = [outputs]
        self.outputs = list(outputs)
        self._get_variables()

    def _get_variables(self):
        """Collect variables, updates and auxiliary variables.

        In addition collects all :class:`.Scan` ops and recurses in the
        respective inner Theano graphs.

        """
        updates = OrderedDict()

        shared_outputs = [o for o in self.outputs if is_trainable_variable(o)]
        usual_outputs = [o for o in self.outputs if not is_trainable_variable(o)]
        variables = shared_outputs

        if usual_outputs:
            # Sort apply nodes topologically, get variables and remove
            # duplicates
            inputs = graph.inputs(self.outputs)
            sorted_apply_nodes = graph.io_toposort(inputs, usual_outputs)
            self.scans = list(_unique([node.op for node in sorted_apply_nodes
                                      if isinstance(node.op, Scan)],
                                      key=lambda op: id(op)))
            self._scan_graphs = [ComputationGraph(scan.outputs)
                                 for scan in self.scans]

            seen = set()
            main_vars = (
                [var for var in list(chain(
                    *[apply_node.inputs for apply_node in sorted_apply_nodes]))
                 if not (var in seen or seen.add(var))] +
                [var for var in self.outputs if var not in seen])

            # While preserving order add auxiliary variables, and collect
            # updates
            seen = set()
            # Intermediate variables could be auxiliary
            seen_avs = set(main_vars)
            variables = []
            for var in main_vars:
                variables.append(var)
                # updates
                _ = getattr(var.tag, 'updates', OrderedDict())
                _ = OrderedDict([(i, j) for i, j in _.iteritems()
                                 if is_variable(i)])
                updates = dict_union(updates, _)
                # auxiliary_variables
                for _ in getattr(var.tag, 'auxiliary_variables', []):
                    if _ not in seen and \
                    not (_ in seen_avs or seen_avs.add(_)):
                        variables.append(_)

        # If trainable_variables is assigned default_update (cloned), we cannot eval()
        # it to get the real numpy array value, hence, try to trace back
        # original shared variable
        def shared_variable_filter(var):
            if is_trainable_variable(var) and hasattr(var, 'default_update'):
                for v in _CREATED_VARIABLE.values():
                    if v.name == var.name and v.ndim == var.ndim:
                        return v
            return var
        self.variables = map(shared_variable_filter, variables)
        self.updates = updates

    # ==================== Get variables ==================== #
    @property
    def inputs(self):
        """ Same as placeholder """
        return self.placeholders

    @property
    def placeholders(self):
        """Inputs to the graph, excluding constants and shared variables."""
        return [var for var in self.variables if is_placeholder(var)]

    @property
    def intermediary_variables(self):
        return [var for var in self.variables if
                var not in self.placeholders and
                var not in self.outputs]

    @property
    def trainable_variables(self):
        return [var for var in self.variables if is_trainable_variable(var)]

    @property
    def parameters(self):
        return [var for var in self.trainable_variables
                if has_roles(var, [PARAMETER])]

    @property
    def auxiliary_variables(self):
        return [var for var in self.variables if has_roles(var, [AUXILIARY])]

    @property
    def dict_of_placeholders(self):
        """Return a mapping from an input name to the input."""
        return {var.name: var for var in self.placeholders}

    # ==================== others ==================== #
    def __iter__(self):
        for v in self.variables:
            yield v

    def __del__(self):
        self.dispose()
        del self.outputs
        del self.variables
