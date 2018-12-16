import io
import textwrap
from collections import defaultdict, OrderedDict
from contextlib import contextmanager

from ..hdl import ast, ir, xfrm


class _Namer:
    def __init__(self):
        super().__init__()
        self._index = 0
        self._names = set()

    def _make_name(self, name, local):
        if name is None:
            self._index += 1
            name = "${}".format(self._index)
        elif not local and name[0] not in "\\$":
            name = "\\{}".format(name)
        while name in self._names:
            self._index += 1
            name = "{}${}".format(name, self._index)
        self._names.add(name)
        return name


class _Bufferer:
    def __init__(self):
        super().__init__()
        self._buffer = io.StringIO()

    def __str__(self):
        return self._buffer.getvalue()

    def _append(self, fmt, *args, **kwargs):
        self._buffer.write(fmt.format(*args, **kwargs))

    def _src(self, src):
        if src:
            self._append("  attribute \\src \"{}\"\n", src.replace("\"", "\\\""))


class _Builder(_Namer, _Bufferer):
    def module(self, name=None, attrs={}):
        name = self._make_name(name, local=False)
        return _ModuleBuilder(self, name, attrs)


class _ModuleBuilder(_Namer, _Bufferer):
    def __init__(self, rtlil, name, attrs):
        super().__init__()
        self.rtlil = rtlil
        self.name  = name
        self.attrs = {"generator": "nMigen"}
        self.attrs.update(attrs)

    def __enter__(self):
        for name, value in self.attrs.items():
            if isinstance(value, str):
                self._append("attribute \\{} \"{}\"\n", name, value.replace("\"", "\\\""))
            else:
                self._append("attribute \\{} {}\n", name, int(value))
        self._append("module {}\n", self.name)
        return self

    def __exit__(self, *args):
        self._append("end\n")
        self.rtlil._buffer.write(str(self))

    def attribute(self, name, value):
        if isinstance(value, str):
            self._append("  attribute \\{} \"{}\"\n", name, value.replace("\"", "\\\""))
        else:
            self._append("  attribute \\{} {}\n", name, int(value))

    def wire(self, width, port_id=None, port_kind=None, name=None, src=""):
        self._src(src)
        name = self._make_name(name, local=False)
        if port_id is None:
            self._append("  wire width {} {}\n", width, name)
        else:
            assert port_kind in ("input", "output", "inout")
            self._append("  wire width {} {} {} {}\n", width, port_kind, port_id, name)
        return name

    def connect(self, lhs, rhs):
        self._append("  connect {} {}\n", lhs, rhs)

    def cell(self, kind, name=None, params={}, ports={}, src=""):
        self._src(src)
        name = self._make_name(name, local=True)
        self._append("  cell {} {}\n", kind, name)
        for param, value in params.items():
            if isinstance(value, str):
                value = repr(value)
            else:
                value = int(value)
            self._append("    parameter \\{} {}\n", param, value)
        for port, wire in ports.items():
            self._append("    connect {} {}\n", port, wire)
        self._append("  end\n")
        return name

    def process(self, name=None, src=""):
        name = self._make_name(name, local=True)
        return _ProcessBuilder(self, name, src)


class _ProcessBuilder(_Bufferer):
    def __init__(self, rtlil, name, src):
        super().__init__()
        self.rtlil = rtlil
        self.name  = name
        self.src   = src

    def __enter__(self):
        self._src(self.src)
        self._append("  process {}\n", self.name)
        return self

    def __exit__(self, *args):
        self._append("  end\n")
        self.rtlil._buffer.write(str(self))

    def case(self):
        return _CaseBuilder(self, indent=2)

    def sync(self, kind, cond=None):
        return _SyncBuilder(self, kind, cond)


class _CaseBuilder:
    def __init__(self, rtlil, indent):
        self.rtlil  = rtlil
        self.indent = indent

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def assign(self, lhs, rhs):
        self.rtlil._append("{}assign {} {}\n", "  " * self.indent, lhs, rhs)

    def switch(self, cond):
        return _SwitchBuilder(self.rtlil, cond, self.indent)


class _SwitchBuilder:
    def __init__(self, rtlil, cond, indent):
        self.rtlil  = rtlil
        self.cond   = cond
        self.indent = indent

    def __enter__(self):
        self.rtlil._append("{}switch {}\n", "  " * self.indent, self.cond)
        return self

    def __exit__(self, *args):
        self.rtlil._append("{}end\n", "  " * self.indent)

    def case(self, value=None):
        if value is None:
            self.rtlil._append("{}case\n", "  " * (self.indent + 1))
        else:
            self.rtlil._append("{}case {}'{}\n", "  " * (self.indent + 1),
                               len(value), value)
        return _CaseBuilder(self.rtlil, self.indent + 2)


class _SyncBuilder:
    def __init__(self, rtlil, kind, cond):
        self.rtlil = rtlil
        self.kind  = kind
        self.cond  = cond

    def __enter__(self):
        if self.cond is None:
            self.rtlil._append("    sync {}\n", self.kind)
        else:
            self.rtlil._append("    sync {} {}\n", self.kind, self.cond)
        return self

    def __exit__(self, *args):
        pass

    def update(self, lhs, rhs):
        self.rtlil._append("      update {} {}\n", lhs, rhs)


def src(src_loc):
    file, line = src_loc
    return "{}:{}".format(file, line)


class _ValueCompilerState:
    def __init__(self, rtlil):
        self.rtlil    = rtlil
        self.wires    = ast.ValueDict()
        self.driven   = ast.ValueDict()
        self.ports    = ast.ValueDict()
        self.sub_name = None

    def add_driven(self, signal, sync):
        self.driven[signal] = sync

    def add_port(self, signal, kind):
        assert kind in ("i", "o", "io")
        if kind == "i":
            kind = "input"
        elif kind == "o":
            kind = "output"
        elif kind == "io":
            kind = "inout"
        self.ports[signal] = (len(self.ports), kind)

    def resolve(self, signal):
        if signal in self.wires:
            return self.wires[signal]

        if signal in self.ports:
            port_id, port_kind = self.ports[signal]
        else:
            port_id = port_kind = None
        if self.sub_name:
            wire_name = "{}_{}".format(self.sub_name, signal.name)
        else:
            wire_name = signal.name

        for attr_name, attr_signal in signal.attrs.items():
            self.rtlil.attribute(attr_name, attr_signal)
        wire_curr = self.rtlil.wire(width=signal.nbits, name=wire_name,
                                    port_id=port_id, port_kind=port_kind,
                                    src=src(signal.src_loc))
        if signal in self.driven:
            wire_next = self.rtlil.wire(width=signal.nbits, name=wire_curr + "$next",
                                        src=src(signal.src_loc))
        else:
            wire_next = None
        self.wires[signal] = (wire_curr, wire_next)

        return wire_curr, wire_next

    def resolve_curr(self, signal):
        wire_curr, wire_next = self.resolve(signal)
        return wire_curr

    @contextmanager
    def hierarchy(self, sub_name):
        try:
            self.sub_name = sub_name
            yield
        finally:
            self.sub_name = None


class _ValueCompiler(xfrm.AbstractValueTransformer):
    def __init__(self, state):
        self.s = state

    def on_unknown(self, value):
        if value is None:
            return None
        else:
            super().on_unknown(value)

    def on_ClockSignal(self, value):
        raise NotImplementedError # :nocov:

    def on_ResetSignal(self, value):
        raise NotImplementedError # :nocov:

    def on_Slice(self, value):
        if value.start == 0 and value.end == len(value.value):
            return self(value.value)
        elif value.start + 1 == value.end:
            return "{} [{}]".format(self(value.value), value.start)
        else:
            return "{} [{}:{}]".format(self(value.value), value.end - 1, value.start)

    def on_Cat(self, value):
        return "{{ {} }}".format(" ".join(reversed([self(o) for o in value.operands])))


class _RHSValueCompiler(_ValueCompiler):
    operator_map = {
        (1, "~"):    "$not",
        (1, "-"):    "$neg",
        (1, "b"):    "$reduce_bool",
        (2, "+"):    "$add",
        (2, "-"):    "$sub",
        (2, "*"):    "$mul",
        (2, "/"):    "$div",
        (2, "%"):    "$mod",
        (2, "**"):   "$pow",
        (2, "<<"):   "$sshl",
        (2, ">>"):   "$sshr",
        (2, "&"):    "$and",
        (2, "^"):    "$xor",
        (2, "|"):    "$or",
        (2, "=="):   "$eq",
        (2, "!="):   "$ne",
        (2, "<"):    "$lt",
        (2, "<="):   "$le",
        (2, ">"):    "$gt",
        (2, ">="):   "$ge",
        (3, "m"):    "$mux",
    }

    def on_Const(self, value):
        if isinstance(value.value, str):
            return "{}'{}".format(value.nbits, value.value)
        else:
            return "{}'{:b}".format(value.nbits, value.value)

    def on_Signal(self, value):
        wire_curr, wire_next = self.s.resolve(value)
        return wire_curr

    def on_Operator_unary(self, value):
        arg, = value.operands
        arg_bits, arg_sign = arg.shape()
        res_bits, res_sign = value.shape()
        res = self.s.rtlil.wire(width=res_bits)
        self.s.rtlil.cell(self.operator_map[(1, value.op)], ports={
            "\\A": self(arg),
            "\\Y": res,
        }, params={
            "A_SIGNED": arg_sign,
            "A_WIDTH": arg_bits,
            "Y_WIDTH": res_bits,
        }, src=src(value.src_loc))
        return res

    def match_shape(self, value, new_bits, new_sign):
        if isinstance(value, ast.Const):
            return self(ast.Const(value.value, (new_bits, new_sign)))

        value_bits, value_sign = value.shape()
        if new_bits <= value_bits:
            return self(ast.Slice(value, 0, new_bits))

        res = self.s.rtlil.wire(width=new_bits)
        self.s.rtlil.cell("$pos", ports={
            "\\A": self(value),
            "\\Y": res,
        }, params={
            "A_SIGNED": value_sign,
            "A_WIDTH": value_bits,
            "Y_WIDTH": new_bits,
        }, src=src(value.src_loc))
        return res

    def on_Operator_binary(self, value):
        lhs, rhs = value.operands
        lhs_bits, lhs_sign = lhs.shape()
        rhs_bits, rhs_sign = rhs.shape()
        if lhs_sign == rhs_sign:
            lhs_wire = self(lhs)
            rhs_wire = self(rhs)
        else:
            lhs_sign = rhs_sign = True
            lhs_bits = rhs_bits = max(lhs_bits, rhs_bits)
            lhs_wire = self.match_shape(lhs, lhs_bits, lhs_sign)
            rhs_wire = self.match_shape(rhs, rhs_bits, rhs_sign)
        res_bits, res_sign = value.shape()
        res = self.s.rtlil.wire(width=res_bits)
        self.s.rtlil.cell(self.operator_map[(2, value.op)], ports={
            "\\A": lhs_wire,
            "\\B": rhs_wire,
            "\\Y": res,
        }, params={
            "A_SIGNED": lhs_sign,
            "A_WIDTH": lhs_bits,
            "B_SIGNED": rhs_sign,
            "B_WIDTH": rhs_bits,
            "Y_WIDTH": res_bits,
        }, src=src(value.src_loc))
        return res

    def on_Operator_mux(self, value):
        sel, lhs, rhs = value.operands
        lhs_bits, lhs_sign = lhs.shape()
        rhs_bits, rhs_sign = rhs.shape()
        res_bits, res_sign = value.shape()
        lhs_bits = rhs_bits = res_bits = max(lhs_bits, rhs_bits, res_bits)
        lhs_wire = self.match_shape(lhs, lhs_bits, lhs_sign)
        rhs_wire = self.match_shape(rhs, rhs_bits, rhs_sign)
        res = self.s.rtlil.wire(width=res_bits)
        self.s.rtlil.cell("$mux", ports={
            "\\A": lhs_wire,
            "\\B": rhs_wire,
            "\\S": self(sel),
            "\\Y": res,
        }, params={
            "WIDTH": res_bits
        }, src=src(value.src_loc))
        return res

    def on_Operator(self, value):
        if len(value.operands) == 1:
            return self.on_Operator_unary(value)
        elif len(value.operands) == 2:
            return self.on_Operator_binary(value)
        elif len(value.operands) == 3:
            assert value.op == "m"
            return self.on_Operator_mux(value)
        else:
            raise TypeError # :nocov:

    def on_Part(self, value):
        raise NotImplementedError

    def on_Repl(self, value):
        return "{{ {} }}".format(" ".join(self(value.value) for _ in range(value.count)))

    def on_ArrayProxy(self, value):
        raise NotImplementedError


class _LHSValueCompiler(_ValueCompiler):
    def on_Const(self, value):
        raise TypeError # :nocov:

    def on_Operator(self, value):
        raise TypeError # :nocov:

    def on_Signal(self, value):
        wire_curr, wire_next = self.s.resolve(value)
        if wire_next is None:
            raise ValueError("Cannot return lhs for non-driven signal {}".format(repr(value)))
        return wire_next

    def on_Part(self, value):
        raise NotImplementedError

    def on_Repl(self, value):
        raise TypeError # :nocov:

    def on_ArrayProxy(self, value):
        raise NotImplementedError


def convert_fragment(builder, fragment, name, top):
    with builder.module(name or "anonymous", attrs={"top": 1} if top else {}) as module:
        compiler_state = _ValueCompilerState(module)
        rhs_compiler   = _RHSValueCompiler(compiler_state)
        lhs_compiler   = _LHSValueCompiler(compiler_state)

        # Register all signals driven in the current fragment. This must be done first, as it
        # affects further codegen; e.g. whether sig$next signals will be generated and used.
        for domain, signal in fragment.iter_drivers():
            compiler_state.add_driven(signal, sync=domain is not None)

        # Transform all signals used as ports in the current fragment eagerly and outside of
        # any hierarchy, to make sure they get sensible (non-prefixed) names.
        for signal in fragment.ports:
            compiler_state.add_port(signal, fragment.ports[signal])
            rhs_compiler(signal)

        # Transform all clocks clocks and resets eagerly and outside of any hierarchy, to make
        # sure they get sensible (non-prefixed) names. This does not affect semantics.
        for domain, _ in fragment.iter_sync():
            cd = fragment.domains[domain]
            rhs_compiler(cd.clk)
            rhs_compiler(cd.rst)

        # Transform all subfragments to their respective cells. Transforming signals connected
        # to their ports into wires eagerly makes sure they get sensible (prefixed with submodule
        # name) names.
        for subfragment, sub_name in fragment.subfragments:
            sub_name, sub_port_map = \
                convert_fragment(builder, subfragment, top=False, name=sub_name)
            with compiler_state.hierarchy(sub_name):
                module.cell(sub_name, name=sub_name, ports={
                    p: rhs_compiler(s) for p, s in sub_port_map.items()
                })

        with module.process() as process:
            with process.case() as case:
                # For every signal in comb domain, assign \sig$next to the reset value.
                # For every signal in sync domains, assign \sig$next to the current value (\sig).
                for domain, signal in fragment.iter_drivers():
                    if domain is None:
                        prev_value = ast.Const(signal.reset, signal.nbits)
                    else:
                        prev_value = signal
                    case.assign(lhs_compiler(signal), rhs_compiler(prev_value))

                # Convert statements into decision trees.
                def _convert_stmts(case, stmts):
                    for stmt in stmts:
                        if isinstance(stmt, ast.Assign):
                            lhs_bits, lhs_sign = stmt.lhs.shape()
                            rhs_bits, rhs_sign = stmt.rhs.shape()
                            if lhs_bits == rhs_bits:
                                rhs_sigspec = rhs_compiler(stmt.rhs)
                            else:
                                # In RTLIL, LHS and RHS of assignment must have exactly same width.
                                rhs_sigspec = rhs_compiler.match_shape(
                                    stmt.rhs, lhs_bits, rhs_sign)
                            case.assign(lhs_compiler(stmt.lhs), rhs_sigspec)

                        elif isinstance(stmt, ast.Switch):
                            with case.switch(rhs_compiler(stmt.test)) as switch:
                                for value, nested_stmts in stmt.cases.items():
                                    with switch.case(value) as nested_case:
                                        _convert_stmts(nested_case, nested_stmts)

                        else:
                            raise TypeError

                _convert_stmts(case, fragment.statements)

            # For every signal in the sync domain, assign \sig's initial value (which will end up
            # as the \init reg attribute) to the reset value.
            with process.sync("init") as sync:
                for domain, signal in fragment.iter_sync():
                    wire_curr, wire_next = compiler_state.resolve(signal)
                    sync.update(wire_curr, rhs_compiler(ast.Const(signal.reset, signal.nbits)))

            # For every signal in every domain, assign \sig to \sig$next. The sensitivity list,
            # however, differs between domains: for comb domains, it is `always`, for sync domains
            # with sync reset, it is `posedge clk`, for sync domains with async rest it is
            # `posedge clk or posedge rst`.
            for domain, signals in fragment.drivers.items():
                triggers = []
                if domain is None:
                    triggers.append(("always",))
                else:
                    cd = fragment.domains[domain]
                    triggers.append(("posedge", compiler_state.resolve_curr(cd.clk)))
                    if cd.async_reset:
                        triggers.append(("posedge", compiler_state.resolve_curr(cd.rst)))

                for trigger in triggers:
                    with process.sync(*trigger) as sync:
                        for signal in signals:
                            wire_curr, wire_next = compiler_state.resolve(signal)
                            sync.update(wire_curr, wire_next)

    # Finally, collect the names we've given to our ports in RTLIL, and correlate these with
    # the signals represented by these ports. If we are a submodule, this will be necessary
    # to create a cell for us in the parent module.
    port_map = OrderedDict()
    for signal in fragment.ports:
        port_map[compiler_state.resolve_curr(signal)] = signal

    return module.name, port_map


def convert(fragment, name="top", **kwargs):
    fragment = fragment.prepare(**kwargs)
    builder = _Builder()
    convert_fragment(builder, fragment, name=name, top=True)
    return str(builder)
