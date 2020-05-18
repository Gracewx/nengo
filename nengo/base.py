from collections import OrderedDict
from copy import copy
import inspect
import sys
import warnings

import numpy as np

import nengo
from nengo.config import Config
from nengo.exceptions import NotAddedToNetworkWarning, ReadonlyError, ValidationError
from nengo.params import (
    Default,
    equal,
    IntParam,
    iter_params,
    NumberParam,
    ObsoleteParam,
    Parameter,
    StringParam,
    Unconfigurable,
)
from nengo.rc import rc
from nengo.utils.numpy import as_shape, maxint, maxseed, is_integer


class NetworkMember(type):
    """A metaclass used to add instances of derived classes to networks.

    Inheriting from this class means that Network.add will be invoked after
    initializing the object, unless add_to_container=False is passed to the
    derived class constructor.
    """

    def __call__(cls, *args, **kwargs):
        """Override default __call__ behavior so that Network.add is called."""
        inst = cls.__new__(cls)
        add_to_container = kwargs.pop("add_to_container", True)
        # Do the __init__ before adding in case __init__ errors out
        inst.__init__(*args, **kwargs)
        if add_to_container:
            nengo.Network.add(inst)
        inst._initialized = True
        return inst


class NengoObject(metaclass=NetworkMember):
    """A base class for Nengo objects.

    Parameters
    ----------
    label : string
        A descriptive label for the object.
    seed : int
        The seed used for random number generation.

    Attributes
    ----------
    label : string
        A descriptive label for the object.
    seed : int
        The seed used for random number generation.
    """

    # Order in which parameters have to be initialized.
    # Missing parameters will be initialized last in an undefined order.
    # This is needed for pickling and copying of Nengo objects when the
    # parameter initialization order matters.
    _param_init_order = []

    label = StringParam("label", default=None, optional=True)
    seed = IntParam("seed", default=None, low=0, high=maxseed, optional=True)

    def __init__(self, label, seed):
        super().__init__()
        self._initialized = False
        self.label = label
        self.seed = seed

    def __getstate__(self):
        state = self.__dict__.copy()
        state["_initialized"] = False

        for attr in self.params:
            param = getattr(type(self), attr)
            if self in param:
                state[attr] = getattr(self, attr)

        return state

    def __setstate__(self, state):
        for attr in self._param_init_order:
            setattr(self, attr, state.pop(attr))

        for attr in self.params:
            if attr in state:
                setattr(self, attr, state.pop(attr))

        for k, v in state.items():
            setattr(self, k, v)

        self._initialized = True
        if len(nengo.Network.context) > 0:
            warnings.warn(NotAddedToNetworkWarning(self))

    def __setattr__(self, name, val):
        initialized = hasattr(self, "_initialized") and self._initialized
        if initialized and not hasattr(self, name):
            warnings.warn(
                "Creating new attribute '%s' on '%s'. "
                "Did you mean to change an existing attribute?" % (name, self),
                SyntaxWarning,
            )
        if val is Default:
            val = Config.default(type(self), name)

        if rc.getboolean("exceptions", "simplified"):
            try:
                super().__setattr__(name, val)
            except ValidationError:
                exc_info = sys.exc_info()
                raise exc_info[1].with_traceback(None)
        else:
            super().__setattr__(name, val)

    def __str__(self):
        return self._str(include_id=not hasattr(self, "label") or self.label is None)

    def __repr__(self):
        return self._str(include_id=True)

    def _str(self, include_id):
        return "<%s%s%s>" % (
            type(self).__name__,
            ""
            if not hasattr(self, "label")
            else " (unlabeled)"
            if self.label is None
            else ' "%s"' % self.label,
            " at 0x%x" % id(self) if include_id else "",
        )

    @property
    def params(self):
        """Returns a list of parameter names that can be set."""
        return list(iter_params(self))

    def copy(self, add_to_container=True):
        with warnings.catch_warnings():
            # We warn when copying since we can't change add_to_container.
            # However, we deal with it here, so we ignore the warning.
            warnings.simplefilter("ignore", category=NotAddedToNetworkWarning)
            c = copy(self)
        if add_to_container:
            nengo.Network.add(c)
        return c


class ObjView:
    """Container for a slice with respect to some object.

    This is used by the __getitem__ of Neurons, Node, and Ensemble, in order
    to pass slices of those objects to Connection. This is a notational
    convenience for creating transforms. See Connection for details.

    Does not currently support any other view-like operations.
    """

    def __init__(self, obj, key=slice(None)):
        self.obj = obj

        # Node.size_in != size_out, so one of these can be invalid
        # NumPy <= 1.8 raises a ValueError instead of an IndexError.
        try:
            self.size_in = np.arange(self.obj.size_in)[key].size
        except (IndexError, ValueError):
            self.size_in = None
        try:
            self.size_out = np.arange(self.obj.size_out)[key].size
        except (IndexError, ValueError):
            self.size_out = None
        if self.size_in is None and self.size_out is None:
            raise IndexError("Invalid slice '%s' of %s" % (key, self.obj))

        if is_integer(key):
            # single slices of the form [i] should be cast into
            # slice objects for convenience
            if key == -1:
                # special case because slice(-1, 0) gives the empty list
                self.slice = slice(key, None)
            else:
                self.slice = slice(key, key + 1)
        else:
            self.slice = key

    def copy(self):
        return copy(self)

    def __len__(self):
        return self.size_out

    def __str__(self):
        return "%s[%s]" % (self.obj, self._slice_string)

    def __repr__(self):
        return "%r[%s]" % (self.obj, self._slice_string)

    @property
    def _slice_string(self):
        if isinstance(self.slice, slice):
            sl_start = "" if self.slice.start is None else self.slice.start
            sl_stop = "" if self.slice.stop is None else self.slice.stop
            if self.slice.step is None:
                return "%s:%s" % (sl_start, sl_stop)
            else:
                return "%s:%s:%s" % (sl_start, sl_stop, self.slice.step)
        else:
            return str(self.slice)


class FrozenObject:
    """An object with parameters that cannot change value after instantiation.

    Since such objects are read-only ("frozen"), they can be safely used in
    multiple locations, compared, etc.
    """

    # Order in which parameters have to be initialized.
    # Missing parameters will be initialized last in an undefined order.
    # This is needed for pickling and copying of Nengo objects when the
    # parameter initialization order matters.
    _param_init_order = []

    def __init__(self):
        self._paramdict = OrderedDict(
            (k, v)
            for k, v in inspect.getmembers(type(self))
            if isinstance(v, Parameter) and not isinstance(v, ObsoleteParam)
        )
        for p in self._params:
            if not p.readonly:
                msg = "All parameters of a FrozenObject must be readonly"
                raise ReadonlyError(attr=p, obj=self, msg=msg)
        self.__argreprs = None

    @property
    def _params(self):
        return list(self._paramdict.values())

    def __eq__(self, other):
        if self is other:  # quick check for speed
            return True
        return type(self) == type(other) and all(
            p.equal(self, other) for p in self._params
        )

    def __hash__(self):
        return hash((type(self), tuple(p.hashvalue(self) for p in self._params)))

    def __getstate__(self):
        d = dict(self.__dict__)
        d.pop("_paramdict")  # do not pickle the param dict itself
        for k in self._paramdict:
            d[k] = getattr(self, k)

        return d

    def __setstate__(self, state):
        FrozenObject.__init__(self)  # set up the param dict

        for attr in self._param_init_order:
            setattr(self, attr, state.pop(attr))
        for attr in set(self._paramdict).difference(self._param_init_order):
            setattr(self, attr, state.pop(attr))

        self.__dict__.update(state)

    def __setattr__(self, name, val):
        if val is Default:
            val = getattr(type(self), name).default

        if rc.getboolean("exceptions", "simplified"):
            try:
                super().__setattr__(name, val)
            except ValidationError:
                exc_info = sys.exc_info()
                raise exc_info[1].with_traceback(None)
        else:
            super().__setattr__(name, val)

    def __repr__(self):
        if isinstance(self._argreprs, str):
            return "<%s at 0x%x>" % (type(self).__name__, id(self))
        return "%s(%s)" % (type(self).__name__, ", ".join(self._argreprs))

    @property
    def _argreprs(self):
        if self.__argreprs is not None:
            return self.__argreprs

        # get arguments to display from __init__ functions
        spec = inspect.getfullargspec(type(self).__init__)
        defaults = {}
        if spec.defaults is not None:
            defaults.update(zip(spec.args[-len(spec.defaults) :], spec.defaults))

        self.__argreprs = []
        for arg in spec.args[1:]:  # start at 1 to drop `self`
            if not hasattr(self, arg):
                # We rely on storing the initial arguments. If we don't have
                # them, we don't auto-generate a repr.
                self.__argreprs = "Cannot find %r" % arg
                break
            value = getattr(self, arg)

            param = self._paramdict.get(arg, None)
            if arg in defaults and defaults[arg] is not Default:
                not_default = not equal(value, defaults[arg])
            elif param is not None and param.default is not Unconfigurable:
                not_default = not equal(value, param.default)
            else:
                not_default = True

            if not_default:
                self.__argreprs.append("%s=%r" % (arg, value))

        return self.__argreprs


class NengoObjectParam(Parameter):
    def __init__(
        self,
        name,
        optional=False,
        readonly=True,
        nonzero_size_in=False,
        nonzero_size_out=False,
    ):
        default = Unconfigurable  # These can't have defaults
        self.nonzero_size_in = nonzero_size_in
        self.nonzero_size_out = nonzero_size_out
        super().__init__(name, default, optional, readonly)

    def coerce(self, instance, nengo_obj):
        nengo_objects = (
            NengoObject,
            ObjView,
            nengo.ensemble.Neurons,
            nengo.connection.LearningRule,
        )
        if not isinstance(nengo_obj, nengo_objects):
            raise ValidationError(
                "'%s' is not a Nengo object" % nengo_obj, attr=self.name, obj=instance
            )
        if self.nonzero_size_in and nengo_obj.size_in < 1:
            raise ValidationError(
                "'%s' must have size_in > 0." % nengo_obj, attr=self.name, obj=instance
            )
        if self.nonzero_size_out and nengo_obj.size_out < 1:
            raise ValidationError(
                "'%s' must have size_out > 0." % nengo_obj, attr=self.name, obj=instance
            )
        return super().coerce(instance, nengo_obj)


class Process(FrozenObject):
    """A general system with input, output, and state.

    For more details on how to use processes and make
    custom process subclasses, see :doc:`examples/advanced/processes`.

    Parameters
    ----------
    default_size_in : int
        Sets the default size in for nodes using this process.
    default_size_out : int
        Sets the default size out for nodes running this process. Also,
        if ``d`` is not specified in `~.Process.run` or `~.Process.run_steps`,
        this will be used.
    default_dt : float
        If ``dt`` is not specified in `~.Process.run`, `~.Process.run_steps`,
        `~.Process.ntrange`, or `~.Process.trange`, this will be used.
    seed : int, optional
        Random number seed. Ensures random factors will be the same each run.

    Attributes
    ----------
    default_dt : float
        If ``dt`` is not specified in `~.Process.run`, `~.Process.run_steps`,
        `~.Process.ntrange`, or `~.Process.trange`, this will be used.
    default_size_in : int
        The default size in for nodes using this process.
    default_size_out : int
        The default size out for nodes running this process. Also, if ``d`` is
        not specified in `~.Process.run` or `~.Process.run_steps`,
        this will be used.
    seed : int or None
        Random number seed. Ensures random factors will be the same each run.
    """

    default_size_in = IntParam("default_size_in", default=0, low=0, readonly=True)
    default_size_out = IntParam("default_size_out", default=1, low=0, readonly=True)
    default_dt = NumberParam(
        "default_dt", default=0.001, low=0, low_open=True, readonly=True
    )
    seed = IntParam(
        "seed", default=None, low=0, high=maxseed, optional=True, readonly=True
    )

    def __init__(
        self,
        default_size_in=Default,
        default_size_out=Default,
        default_dt=Default,
        seed=Default,
    ):
        super().__init__()
        self.default_size_in = default_size_in
        self.default_size_out = default_size_out
        self.default_dt = default_dt
        self.seed = seed

    def apply(self, x, d=None, dt=None, rng=np.random, copy=True, **kwargs):
        """Run process on a given input.

        Keyword arguments that do not appear in the parameter list below
        will be passed to the ``make_step`` function of this process.

        Parameters
        ----------
        x : ndarray
            The input signal given to the process.
        d : int, optional
            Output dimensionality. If None, ``default_size_out`` will be used.
        dt : float, optional
            Simulation timestep. If None, ``default_dt`` will be used.
        rng : `numpy.random.mtrand.RandomState`
            Random number generator used for stochstic processes.
        copy : bool, optional
            If True, a new output array will be created for output.
            If False, the input signal ``x`` will be overwritten.
        """
        shape_in = as_shape(np.asarray(x[0]).shape, min_dim=1)
        shape_out = as_shape(self.default_size_out if d is None else d)
        dt = self.default_dt if dt is None else dt
        rng = self.get_rng(rng)
        state = self.make_state(shape_in, shape_out, dt)
        step = self.make_step(shape_in, shape_out, dt, rng, state, **kwargs)
        output = np.zeros((len(x),) + shape_out) if copy else x
        for i, xi in enumerate(x):
            output[i] = step((i + 1) * dt, xi)
        return output

    def get_rng(self, rng):
        """Get a properly seeded independent RNG for the process step.

        Parameters
        ----------
        rng : `numpy.random.mtrand.RandomState`
            The parent random number generator to use if the seed is not set.
        """
        seed = rng.randint(maxint) if self.seed is None else self.seed
        return np.random.RandomState(seed)

    def make_state(self, shape_in, shape_out, dt, dtype=None):
        """Get a dictionary of signals to represent the state of this process.

        The builder uses this to allocate memory for the process state, so
        that the state can be represented as part of the whole simulator state.

        .. versionadded:: 3.0.0

        Parameters
        ----------
        shape_in : tuple
            The shape of the input signal.
        shape_out : tuple
            The shape of the output signal.
        dt : float
            The simulation timestep.
        dtype : `numpy.dtype`
            The data type requested by the builder. If `None`, then this
            function is free to choose the best type for the signals involved.

        Returns
        -------
        initial_state : {string: `numpy.ndarray`}
            A dictionary mapping keys to arrays containing the initial state
            values. The keys will be used to identify the signals in
            `.Process.make_step`.
        """
        return {}  # Implement if the process has a state

    def make_step(self, shape_in, shape_out, dt, rng, state):
        """Create function that advances the process forward one time step.

        This must be implemented by all custom processes. The parameters below
        indicate what information is provided by the builder.

        Parameters
        ----------
        shape_in : tuple
            The shape of the input signal.
        shape_out : tuple
            The shape of the output signal.
        dt : float
            The simulation timestep.
        rng : `numpy.random.mtrand.RandomState`
            A random number generator.
        state : {string: `numpy.ndarray`}
            A dictionary mapping keys to signals, where the signals fully
            represent the state of the process. The signals are initialized
            by `.Process.make_state`.

            .. versionadded:: 3.0.0
        """
        raise NotImplementedError("Process must implement `make_step` method.")

    def run(self, t, d=None, dt=None, rng=np.random, **kwargs):
        """Run process without input for given length of time.

        Keyword arguments that do not appear in the parameter list below
        will be passed to the ``make_step`` function of this process.

        Parameters
        ----------
        t : float
            The length of time to run.
        d : int, optional
            Output dimensionality. If None, ``default_size_out`` will be used.
        dt : float, optional
            Simulation timestep. If None, ``default_dt`` will be used.
        rng : `numpy.random.mtrand.RandomState`
            Random number generator used for stochstic processes.
        """
        dt = self.default_dt if dt is None else dt
        n_steps = int(np.round(float(t) / dt))
        return self.run_steps(n_steps, d=d, dt=dt, rng=rng, **kwargs)

    def run_steps(self, n_steps, d=None, dt=None, rng=np.random, **kwargs):
        """Run process without input for given number of steps.

        Keyword arguments that do not appear in the parameter list below
        will be passed to the ``make_step`` function of this process.

        Parameters
        ----------
        n_steps : int
            The number of steps to run.
        d : int, optional
            Output dimensionality. If None, ``default_size_out`` will be used.
        dt : float, optional
            Simulation timestep. If None, ``default_dt`` will be used.
        rng : `numpy.random.mtrand.RandomState`
            Random number generator used for stochstic processes.
        """
        shape_in = as_shape(0)
        shape_out = as_shape(self.default_size_out if d is None else d)
        dt = self.default_dt if dt is None else dt
        rng = self.get_rng(rng)
        state = self.make_state(shape_in, shape_out, dt)
        step = self.make_step(shape_in, shape_out, dt, rng, state, **kwargs)
        output = np.zeros((n_steps,) + shape_out)
        for i in range(n_steps):
            output[i] = step((i + 1) * dt)
        return output

    def ntrange(self, n_steps, dt=None):
        """Create time points corresponding to a given number of steps.

        Parameters
        ----------
        n_steps : int
            The given number of steps.
        dt : float, optional
            Simulation timestep. If None, ``default_dt`` will be used.
        """
        dt = self.default_dt if dt is None else dt
        return dt * np.arange(1, n_steps + 1)

    def trange(self, t, dt=None):
        """Create time points corresponding to a given length of time.

        Parameters
        ----------
        t : float
            The given length of time.
        dt : float, optional
            Simulation timestep. If None, ``default_dt`` will be used.
        """
        dt = self.default_dt if dt is None else dt
        n_steps = int(np.round(float(t) / dt))
        return self.ntrange(n_steps, dt=dt)


class ProcessParam(Parameter):
    """Must be a Process."""

    def coerce(self, instance, process):
        self.check_type(instance, process, Process)
        return super().coerce(instance, process)
