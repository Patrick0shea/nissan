"""A port of opentrons 9.0.0 `TransferPlan` (protocols/advanced_control/transfers/transfer.py).

`plan()` turns `transfer` / `distribute` / `consolidate` into the commands Opentrons would execute.
Like the original, the plan reads the pipette's current volume between commands, so we track it as
commands are emitted.

Every decision that depends on a volume goes through `decide`, a callback that settles a
condition: concretely, with Z3 over the parameter domain, or by asking the front end to
enumerate. Volumes may therefore be numbers or Z3 terms.

The port is checked against the real `TransferPlan` by a differential test
(`scripts/diff_transfers.py`, run in the baseline venv, D-020).

Not ported (`NotPorted`, reported as unsupported): `gradient_function`, `CUSTOM_LOCATION` blow-out
(it cannot be reached from transfer() keyword arguments), partial-nozzle configurations
(API >= 2.18) and liquid-class transfers.
"""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from otverify.smt import is_sym, to_real

Num = Any  # float | int | z3.ArithRef
Location = Any  # the front end's well or trash value
Cmd = tuple[Any, ...]  # (method, *args), e.g. ("aspirate", 50.0, well)

# Keyword arguments transfer() reads (opentrons 9.0.0 TransferArgs). Anything else is accepted
# by **kwargs and silently ignored.
KNOWN_KWARGS = {
    "new_tip",
    "trash",
    "touch_tip",
    "blow_out",
    "blowout_location",
    "mix_before",
    "mix_after",
    "disposal_volume",
    "air_gap",
    "carryover",
    "gradient_function",
}

MAX_CHUNKS = 10_000


class PlanError(Exception):
    """Opentrons would raise: the protocol crashes here. `commands` were executed first."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.commands: list[Cmd] = []


class NotPorted(Exception):
    """A transfer feature this port does not model (an analysis gap, not a protocol bug)."""


@dataclass(frozen=True)
class Options:
    mode: str  # "transfer" | "distribute" | "consolidate"
    new_tip: str = "once"  # "once" | "always" | "never"
    trash: bool = True  # False: return tips to the rack
    touch_tip: bool = False
    air_gap: Num = 0
    disposal: Num = 0
    mix_before: tuple[int, Num] | None = None  # (repetitions, volume), already validated
    mix_after: tuple[int, Num] | None = None
    blow_out: str | None = None  # "source" | "dest" | "trash"


def mix_requested(value: object) -> bool:
    """As opentrons `mix_from_kwargs._mix_requested`: not given, None or (0, 0) means no mix."""
    return value is not None and value != (0, 0)


def options_from_kwargs(
    mode: str,
    kwargs: dict[str, Any],
    api: tuple[int, int],
    pipette_min_volume: float,
    decide: Callable[[Any], bool],
) -> Options:
    """Options as InstrumentContext.transfer/distribute/consolidate (opentrons 9.0.0) build them.

    Raises PlanError where Opentrons raises, and for what we do not port."""
    kwargs = dict(kwargs)
    if mode == "distribute":
        kwargs.setdefault("disposal_volume", pipette_min_volume)
        kwargs["mix_after"] = (0, 0)
    elif mode == "consolidate":
        kwargs["mix_before"] = (0, 0)
        kwargs["disposal_volume"] = 0
    if kwargs.get("gradient_function"):
        raise NotPorted("gradient_function not modelled")
    location = kwargs.get("blowout_location")
    if location and api < (2, 8):
        raise PlanError("blowout_location needs API >= 2.8 (ValueError)")
    if mode == "consolidate" and location == "source well":
        raise PlanError("blowout location for consolidate cannot be source well (ValueError)")
    if mode == "distribute" and location == "destination well":
        raise PlanError("blowout location for distribute cannot be destination well (ValueError)")
    if location and location not in ("source well", "destination well", "trash"):
        raise PlanError(f"invalid blowout_location {location!r} (ValueError)")
    blow_out = None
    if kwargs.get("blow_out"):
        # Without a location: source well if the tip holds liquid when transfer() is called,
        # else the trash. We assume an empty tip at that point (D-020).
        blow_out = {
            None: "trash",
            "": "trash",
            "source well": "source",
            "destination well": "dest",
            "trash": "trash",
        }[location]
    new_tip = kwargs.get("new_tip")
    if isinstance(new_tip, str):
        if new_tip.upper() not in ("ONCE", "ALWAYS", "NEVER"):
            raise PlanError(f"invalid new_tip {new_tip!r} (KeyError)")
        new_tip = new_tip.lower()
    else:
        new_tip = "once"

    def mix(opt: str) -> tuple[int, Num] | None:
        value = kwargs.get(opt)
        if isinstance(value, list):
            value = tuple(value)
        if not mix_requested(value):
            return None
        if not isinstance(value, tuple) or len(value) != 2:
            raise NotPorted(f"{opt} that is not a (repetitions, volume) pair")
        reps, vol = value
        # transfer() passes mix options with falsy values dropped: mix() then uses its own
        # defaults, repetitions=1 and volume=None (the full working volume).
        return (reps if reps else 1, vol if vol is not None and decide(vol != 0) else None)

    disposal = kwargs.get("disposal_volume")
    return Options(
        mode=mode,
        new_tip=new_tip,
        trash=bool(kwargs.get("trash", True)),
        touch_tip=bool(kwargs.get("touch_tip")),
        air_gap=kwargs.get("air_gap", 0),
        disposal=0 if disposal is None else disposal,
        mix_before=mix("mix_before"),
        mix_after=mix("mix_after"),
        blow_out=blow_out,
    )


def plan(
    volume: Num | Sequence[Num],
    sources: Location | Sequence[Any],
    dests: Location | Sequence[Any],
    opts: Options,
    *,
    pipette_max: float,
    tip_max: float,
    channels: int,
    api: tuple[int, int],
    valid_row: Callable[[Location], bool],
    decide: Callable[[Any], bool],
    trash: Location,
) -> list[Cmd]:
    planner = _Planner(
        volume, sources, dests, opts, pipette_max, tip_max, channels, api, valid_row, decide, trash
    )
    return planner.commands()


class _Planner:
    def __init__(
        self,
        volume: Num | Sequence[Num],
        sources: Any,
        dests: Any,
        opts: Options,
        pipette_max: float,
        tip_max: float,
        channels: int,
        api: tuple[int, int],
        valid_row: Callable[[Location], bool],
        decide: Callable[[Any], bool],
        trash: Location,
    ) -> None:
        self.opts = opts
        self.pipette_max = pipette_max  # instr.max_volume
        self.max_volume = tip_max  # the max_volume argument: min(tip, pipette) or working volume
        self.api = api
        self.decide = decide
        self.trash = trash
        self.cur: Num = 0  # instr.current_volume, assumed 0 when the transfer starts (D-020)
        self.out: list[Cmd] = []
        if opts.air_gap is not None and (
            decide(opts.air_gap < 0) or decide(opts.air_gap >= tip_max)
        ):
            raise PlanError(
                f"air_gap must be between 0uL and the pipette's expected working volume, "
                f"{tip_max}uL (ValueError)"
            )
        if channels > 1:
            self.sources = self._multichannel(sources, valid_row, "source")
            self.dests = self._multichannel(dests, valid_row, "target")
        else:
            self.sources = self._normalise(sources)
            self.dests = self._normalise(dests)
        total = max(len(self.sources), len(self.dests))
        self.volumes = self._volume_list(volume, total)

    # ---- inputs -------------------------------------------------------------------------------

    @staticmethod
    def _normalise(x: Any) -> list[Any]:
        if isinstance(x, tuple):
            raise NotPorted("a tuple of wells (Opentrons only accepts a well or a list)")
        if isinstance(x, list):
            if not x:
                raise PlanError("empty well list (IndexError)")
            if isinstance(x[0], list):
                return [w for group in x for w in group]
            return x
        return [x]

    def _multichannel(self, x: Any, valid_row: Callable[[Location], bool], what: str) -> list:
        if isinstance(x, list) and x and isinstance(x[0], list):
            x = [w for group in x for w in group]
        elif not isinstance(x, list):
            x = [x]
        kept = [w for w in x if valid_row(w)]
        if self.api >= (2, 2) and not kept:
            raise PlanError(f"Invalid {what} for multichannel transfer (RuntimeError)")
        return kept

    def _volume_list(self, volume: Any, total: int) -> list[Num]:
        if isinstance(volume, bool):
            raise NotPorted("volume is a bool")
        if isinstance(volume, int | float) or is_sym(volume):
            return [volume if is_sym(volume) else float(volume)] * total
        if isinstance(volume, tuple):
            lo, hi = volume[0], volume[-1]
            if total < 2:
                raise PlanError("volume gradient over fewer than 2 transfers (ZeroDivisionError)")
            return [(i / (total - 1)) * (hi - lo) + lo for i in range(total)]
        if isinstance(volume, list):
            if len(volume) != total:
                raise PlanError("List of volumes should be equal to number of transfers")
            return volume
        raise PlanError(f"Volume expected as a number or List or tuple but got {volume!r}")

    # ---- the plan -----------------------------------------------------------------------------

    def commands(self) -> list[Cmd]:
        once = self.opts.new_tip == "once"
        try:
            if once:
                self.emit("pick_up_tip")
            {
                "transfer": self._plan_transfer,
                "distribute": self._plan_distribute,
                "consolidate": self._plan_consolidate,
            }[self.opts.mode]()
        except PlanError as error:
            # Opentrons builds the plan lazily: what was emitted before the error has run.
            error.commands = self.out
            raise
        if once:
            self.emit("drop_tip" if self.opts.trash else "return_tip")
        return self.out

    def _check_volume_parameters(self) -> None:
        d, a, m = self.opts.disposal, self.opts.air_gap, self.pipette_max
        if self.decide(a >= m):
            raise PlanError("The air gap must be less than the maximum volume of the pipette")
        if self.decide(d >= m):
            raise PlanError("The disposal volume must be less than the maximum volume")
        if self.decide(d + a >= m):
            raise PlanError("The sum of the air gap and disposal volume must be less than max")

    def _expand(self, targets: Sequence[Any], max_volume: Num) -> Iterator[tuple[Num, Any]]:
        """`common.expand_for_volume_constraints`."""
        if not self.decide(max_volume > 0):
            raise PlanError("max volume for splitting is not positive (AssertionError)")
        for volume, target in zip(self.volumes, targets, strict=False):
            chunks = 0
            while self.decide(volume > max_volume * 2):
                yield max_volume, target
                volume = volume - max_volume
                chunks += 1
                if chunks > MAX_CHUNKS:
                    raise NotPorted("too many chunks")
            if self.decide(volume > max_volume):
                volume = to_real(volume) / 2
                yield volume, target
            yield volume, target

    def _extend(self) -> tuple[list[Any], list[Any]]:
        sources, targets = self.sources, self.dests
        if not sources or not targets:
            raise PlanError("empty source or destination well list")
        if len(sources) < len(targets):
            if len(targets) % len(sources):
                raise PlanError("Source and destination lists must be divisible (ValueError)")
            sources = [s for s in sources for _ in range(len(targets) // len(sources))]
        elif len(sources) > len(targets):
            if len(sources) % len(targets):
                raise PlanError("Source and destination lists must be divisible (ValueError)")
            targets = [t for t in targets for _ in range(len(sources) // len(targets))]
        return sources, targets

    def _plan_transfer(self) -> None:
        sources, dests = self._extend()
        self._check_volume_parameters()
        o = self.opts
        split = self.pipette_max - o.disposal - o.air_gap
        for step_vol, (src, dest) in self._expand(list(zip(sources, dests, strict=True)), split):
            if o.new_tip == "always":
                self.emit("pick_up_tip")
            max_vol = self.max_volume - o.disposal - o.air_gap
            if not self.decide(max_vol > 0) and self.decide(step_vol > 0):
                raise PlanError("tip too small for the air gap and disposal volume (endless loop)")
            xferred: Num = 0.0
            chunks = 0
            while self.decide(xferred < step_vol):
                rest = step_vol - xferred
                vol = max_vol if self.decide(max_vol <= rest) else rest
                self._aspirate_actions(vol, src)
                self._dispense_actions(vol, dest, src)
                xferred = xferred + vol
                chunks += 1
                if chunks > MAX_CHUNKS:
                    raise NotPorted("too many chunks")
            self._new_tip_action()

    def _volume_not_zero(self, volume: Num) -> bool:
        return self.api < (2, 8) or self.decide(volume > 0)

    def _plan_distribute(self) -> None:
        self._check_volume_parameters()
        o = self.opts
        plan_iter = self._expand(self.dests, self.pipette_max - o.disposal - o.air_gap)
        current = next(plan_iter, None)
        if current is None:
            raise PlanError("no destinations (StopIteration)")
        if o.new_tip == "always":
            self.emit("pick_up_tip")
        done = False
        while not done:
            grouped: list[tuple[Num, Any]] = []
            while self.decide(
                _sum(grouped) + o.disposal + o.air_gap + current[0] <= self.max_volume
            ):
                if self._volume_not_zero(current[0]):
                    grouped.append(current)
                nxt = next(plan_iter, None)
                if nxt is None:
                    done = True
                    break
                current = nxt
            if not grouped:
                break
            self._aspirate_actions(_sum(grouped) + o.disposal, self.sources[0])
            for i, (vol, dest) in enumerate(grouped):
                last = i == len(grouped) - 1
                self._dispense_actions(vol, dest, self.sources[0], is_disp_next=not last)
        self._new_tip_action()

    def _plan_consolidate(self) -> None:
        o = self.opts
        plan_iter = self._expand(self.sources, self.pipette_max)
        current = next(plan_iter, None)
        if current is None:
            raise PlanError("no sources (StopIteration)")
        if o.new_tip == "always":
            self.emit("pick_up_tip")
        done = False
        while not done:
            grouped: list[tuple[Num, Any]] = []
            while self.decide(
                _sum(grouped) + o.disposal + o.air_gap * len(grouped) + current[0]
                <= self.max_volume
            ):
                if self._volume_not_zero(current[0]):
                    grouped.append(current)
                nxt = next(plan_iter, None)
                if nxt is None:
                    done = True
                    break
                current = nxt
            if not grouped:
                break
            for vol, src in grouped:
                self._aspirate_actions(vol, src)
            total = _sum([(v + o.air_gap, w) for v, w in grouped]) - o.air_gap
            self._dispense_actions(total, self.dests[0], None)
        self._new_tip_action()

    # ---- actions ------------------------------------------------------------------------------

    def _truthy(self, v: Num) -> bool:
        return self.decide(v != 0)

    def _aspirate_actions(self, vol: Num, loc: Location) -> None:
        o = self.opts
        if o.mix_before is not None and self.decide(self.cur == 0):
            self.emit("mix", *o.mix_before, loc)
        self.emit("aspirate", vol, loc)
        if self._truthy(o.air_gap):
            self.emit("air_gap", o.air_gap)
        if o.touch_tip:
            self.emit("touch_tip")

    def _dispense_actions(
        self, vol: Num, dest: Location, src: Location | None, is_disp_next: bool = False
    ) -> None:
        o = self.opts
        if self._truthy(o.air_gap):
            vol = vol + o.air_gap
        self.emit("dispense", vol, dest)
        if is_disp_next:
            if self._truthy(o.air_gap):
                self.emit("air_gap", o.air_gap)
            if o.touch_tip:
                self.emit("touch_tip")
            return
        if o.mix_after is not None and self.decide(self.cur == 0):
            self.emit("mix", *o.mix_after, dest)
        if o.touch_tip:
            self.emit("touch_tip")
        if o.blow_out == "source":
            self.emit("blow_out", src)
        elif o.blow_out == "dest":
            self.emit("blow_out", dest)
        elif o.blow_out == "trash" or self._truthy(o.disposal):
            self.emit("blow_out", self.trash)

    def _new_tip_action(self) -> None:
        if self.opts.new_tip == "always":
            self.emit("drop_tip" if self.opts.trash else "return_tip")

    # ---- emitting, and the pipette's current volume -------------------------------------------

    def emit(self, method: str, *args: Any) -> None:
        self.out.append((method, *args))
        if method in ("pick_up_tip", "drop_tip", "return_tip", "blow_out"):
            self.cur = 0
        elif method == "aspirate":
            vol = args[0]
            if self.api < (2, 16) and self.decide(vol == 0):
                self.cur = self.max_volume  # aspirate(0) fills the tip below API 2.16
            else:
                self.cur = self.cur + vol
        elif method == "air_gap":
            self.cur = self.cur + args[0]
        elif method == "dispense":
            vol = args[0]
            zero_is_all = self.api <= (2, 16) and self.decide(vol == 0)
            self.cur = 0 if zero_is_all or self.decide(vol >= self.cur) else self.cur - vol
        elif method == "mix":
            if args[1] is None:
                self.cur = 0  # aspirate(None) fills the tip, dispense(None) empties it


def _sum(grouped: Sequence[tuple[Num, Any]]) -> Num:
    total: Num = 0
    for vol, _ in grouped:
        total = total + vol
    return total
