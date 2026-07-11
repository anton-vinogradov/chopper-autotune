"""KlipperScreen panel for chopper-autotune: a one-tap app to tune the motors, save the
result, and play the audible before/after show from the touchscreen.

Buttons send the CHOPPER_* macros over the Klippy websocket; those macros launch the tool
detached, so the screen stays responsive. Progress comes back on display_status.message
(the tool's M117) and is shown live under the buttons; when idle the same area shows each
motor's default vs currently-saved registers.
"""
import json
import os

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango
from ks_includes.screen_panel import ScreenPanel

REGISTERS = ("driver_tbl", "driver_toff", "driver_hstrt", "driver_hend")
DRIVERS = ("tmc2209", "tmc2208", "tmc2240", "tmc5160", "tmc2130", "tmc2660")
DEFAULT = (2, 3, 5, 0)  # Klipper's chopper defaults — the "before" the show compares against
STATE = os.path.expanduser("~/printer_data/config/chopper-autotune/state.json")
BELTS_STATE = os.path.expanduser("~/printer_data/config/chopper-autotune/belts.json")
ENVELOPE_STATE = os.path.expanduser("~/printer_data/config/chopper-autotune/envelope.json")
MAP_STATE = os.path.expanduser("~/printer_data/config/chopper-autotune/map.json")


class Panel(ScreenPanel):

    def __init__(self, screen, title):
        super().__init__(screen, title or _("Chopper Autotune"))

        # the chopper is a property of the motor: A = stepper_x, B = stepper_y on any
        # kinematics (on CoreXY those two steppers literally are motors A and B);
        # the extruder's TMC section is "tmcXXXX extruder", so the same lookup works
        self.motors = (("stepper_x", "A"), ("stepper_y", "B"), ("extruder", "E"))

        # the top rows ARE the plan: mechanics first (belts), tune the gantry motors,
        # spend the bought torque margin on a cooler current (then Tune AGAIN — the
        # chopper optimum depends on the current; that sandwich is measured, see the
        # README plan), then the extruder; Envelope is the optional headroom check, and
        # acceleration ringing belongs to Klipper's own Input Shaper panel afterwards
        actions = [
            ("move", _("1 Belts"), "color1", "CHOPPER_BELTS",
             _("Step 1 — mechanics first. Measure belt tension: follow the display, pluck each belt's long front span hard, like a guitar string, twice per belt.")),
            ("fine-tune", _("2 Tune"), "color2", "CHOPPER_TUNE MOTOR=AB",
             _("Step 2 — tune both gantry motors' choppers at their resonances (~20 minutes of movement). Motor B is seeded with A's winner. Then Save, and continue with 3 Current.")),
            ("settings", _("3 Current"), "color3", "CHOPPER_CURRENT SAVE=1",
             _("Step 3 — find the minimal safe run current (worst-case stress + endstop referee) and WRITE it into the config. Afterwards run 2 Tune again: the chopper optimum depends on the current.")),
            ("extrude", _("4 Extruder"), "color4", "CHOPPER_EXTRUDER",
             _("Step 4 — tune the extruder chopper. The hotend will HEAT to 200C (filament stays in), ~10 minutes; the heater turns off when done. Then Save.")),
            # row 2 — the optional check + supporting actions
            ("increase", _("5 Envelope"), "color1", "CHOPPER_ENVELOPE",
             _("Optional check — verify the speed/acceleration headroom: worst-case stress with the endstop referee, ~7 minutes. Finish the plan with Klipper's Input Shaper panel.")),
            ("complete", _("Save"), "color2", "CHOPPER_SAVE",
             _("Save the latest tuning result for each motor (and the extruder's last winner) into the config and restart Klipper?")),
            ("resume", _("Show"), "color3", "CHOPPER_DEMO MOTOR=AB ROUNDS=2 REPEATS=2",
             _("Play the driver defaults against the tuned registers on both motors so you can hear the difference?")),
            ("move", _("Motor A"), "color4", "CHOPPER_BELTS SHOW=A",
             _("Jog motor A (stepper_x) briefly so you can see which motor and belt it is, then release the motors?")),
            ("move", _("Motor B"), "color1", "CHOPPER_BELTS SHOW=B",
             _("Jog motor B (stepper_y) briefly so you can see which motor and belt it is, then release the motors?")),
            ("move", _("Map"), "color2", "CHOPPER_MAP",
             _("Map vibration vs speed on the current registers (~2 min, motor A)? Shows which speeds ring (VFAs) and which stay quiet; the peaks land in Results.")),
        ]

        grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, vexpand=False)
        for index, (icon, label, style, command, confirm) in enumerate(actions):
            button = self._gtk.Button(icon, label, style)
            button.connect("clicked", self.run, command, confirm)
            grid.attach(button, index % 4, index // 4, 1, 1)
        # local buttons (no printer action): Results shows everything measured so far
        results = self._gtk.Button("info", _("Results"), "color1")
        results.connect("clicked", self.show_results)
        grid.attach(results, len(actions) % 4, len(actions) // 4, 1, 1)
        stop = self._gtk.Button("stop", _("Stop"), "color4")
        stop.connect("clicked", self.stop)
        # 4 columns keep the buttons to three rows, leaving the status area its height
        grid.attach(stop, (len(actions) + 1) % 4, (len(actions) + 1) // 4, 1, 1)

        self.status = Gtk.Label(hexpand=True, vexpand=True, halign=Gtk.Align.CENTER,
                                valign=Gtk.Align.CENTER, wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR)
        # three button rows leave little height: scroll the status area instead of
        # clipping the register table off the bottom of the screen
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.status)

        self.content.add(grid)
        self.content.add(scroll)
        self.content.show_all()

        self.show_status(self._printer.get_stat("display_status", "message"))

    def run(self, widget, command, confirm):
        self._screen._confirm_send_action(widget, confirm, "printer.gcode.script",
                                          {"script": command})

    def stop(self, widget):
        self._screen._ws.klippy.gcode_script("CHOPPER_STOP")
        # the tool restores registers and re-homes before exiting (~10 s), so give
        # immediate feedback that the tap registered
        self.show_status(_("Stopping — restoring registers and re-homing…"))

    def process_update(self, action, data):
        # fire whenever the message field is in the update (progress-only updates omit it),
        # including a clear back to None/"" so the panel returns to the register table
        if action == "notify_status_update" and "message" in data.get("display_status", {}):
            self.show_status(data["display_status"]["message"])

    def show_status(self, message):
        if message and message.strip():
            self.status.set_markup(f"<span size='large'>{GLib.markup_escape_text(message.strip())}</span>")
        else:
            self.status.set_markup(
                f"<span font_family='monospace' size='medium'>{GLib.markup_escape_text(self.register_table())}</span>")

    def show_results(self, widget=None):
        # on-demand summary of everything measured so far; a later status update
        # overwrites it, tapping Results brings it back
        self.status.set_markup(
            f"<span font_family='monospace' size='medium'>{GLib.markup_escape_text(self.results_text())}</span>")

    def results_text(self):
        lines = [self.register_table()]
        currents = []
        for stepper, name in self.motors:
            section = self.tmc_section(stepper)
            if section and section.get("run_current") is not None:
                currents.append("%s %sA" % (name, section["run_current"]))
        if currents:
            lines.append(_("run_current: ") + "  ".join(currents))
        belts = self.load_json(BELTS_STATE)
        if belts.get("A") and belts.get("B"):
            # tension ~ f^2: a 3% frequency gap is a ~6% tension gap — show percent,
            # a bare 0.94 next to the frequencies reads as a contradiction
            gap = ((belts["A"] / belts["B"]) ** 2 - 1) * 100
            lines.append(_("belts: ") + "A %.0f / B %.0f Hz (%s %+.0f%%, T~f²)"
                         % (belts["A"], belts["B"], _("tension"), gap))
        envelope = self.load_json(ENVELOPE_STATE)
        if envelope:
            # "350+" = held the whole tested range (the motor is not the limit there)
            lines.append(_("envelope: ") + "  ".join(
                "%s %s mm/s, %s acc" % (name, values.get("speed"), values.get("accel"))
                for name, values in envelope.items()))
        vfa_map = self.load_json(MAP_STATE)
        for name, entry in vfa_map.items():
            peaks = ",".join(str(s) for s in entry.get("peaks", [])) or "—"
            dips = ",".join(str(s) for s in entry.get("dips", [])) or "—"
            advice = entry.get("advice")
            lines.append(_("map ") + "%s: %s %s · %s %s%s"
                         % (name, _("peaks"), peaks, _("dips"), dips,
                            " · %s" % advice if advice else ""))
        return "\n".join(lines)

    def register_table(self):
        default = "/".join(str(v) for v in DEFAULT)
        state = self.load_state()
        rows = ["%-3s %7s   %-10s %s" % ("", _("default"), _("tuned"), _("vibration"))]
        for stepper, name in self.motors:
            tuned = self.tuned_registers(stepper)
            axis = stepper.rsplit("_", 1)[-1]
            rows.append("%-3s %7s → %-10s %s" % (name, default, tuned or _("untuned"),
                                                 self.noise_change(axis, tuned, state)))
        return "\n".join(rows)

    def tmc_section(self, stepper):
        for driver in DRIVERS:
            section = self._printer.get_config_section(f"{driver} {stepper}")
            if section:
                return section
        return None

    def tuned_registers(self, stepper):
        section = self.tmc_section(stepper)
        if section:
            values = [section.get(reg) for reg in REGISTERS]
            if all(value is not None for value in values):
                return "/".join(str(value) for value in values)
        return ""

    def noise_change(self, axis, tuned, state):
        entry = state.get(axis)
        if not tuned or not entry or entry.get("regs") != tuned or not entry.get("quieter"):
            return ""
        pct = round((1 - 1 / entry["quieter"]) * 100)
        # quieter < 1 means the demo measured MORE vibration: show +N%, not "--N%"
        return "-%d%%" % pct if pct >= 0 else "+%d%%" % -pct

    @staticmethod
    def load_json(path):
        try:
            with open(path) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}

    @classmethod
    def load_state(cls):
        return cls.load_json(STATE)
