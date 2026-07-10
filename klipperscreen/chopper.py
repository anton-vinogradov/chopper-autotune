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


class Panel(ScreenPanel):

    def __init__(self, screen, title):
        super().__init__(screen, title or _("Chopper Autotune"))

        # the chopper is a property of the motor: A = stepper_x, B = stepper_y on any
        # kinematics (on CoreXY those two steppers literally are motors A and B);
        # the extruder's TMC section is "tmcXXXX extruder", so the same lookup works
        self.motors = (("stepper_x", "A"), ("stepper_y", "B"), ("extruder", "E"))

        # the top row IS the plan: mechanics first (belts), then the gantry motors, then
        # the extruder, then verify the speed/accel headroom — finish with Klipper's own
        # Input Shaper panel (ringing from acceleration is its job, not the chopper's)
        actions = [
            # row 1 — the plan, in order
            ("move", _("1 Belts"), "color1", "CHOPPER_BELTS",
             _("Step 1 — mechanics first. Measure belt tension: follow the display, pluck each belt's long front span hard, like a guitar string, twice per belt.")),
            ("fine-tune", _("2 Tune"), "color2", "CHOPPER_TUNE MOTOR=AB",
             _("Step 2 — tune both gantry motors' choppers at their resonances (~20 minutes of movement). Motor B is seeded with A's winner. Then Save.")),
            ("extrude", _("3 Extruder"), "color3", "CHOPPER_EXTRUDER",
             _("Step 3 — tune the extruder chopper. The hotend will HEAT to 200C (filament stays in), ~10 minutes; the heater turns off when done. Then Save.")),
            ("increase", _("4 Envelope"), "color4", "CHOPPER_ENVELOPE",
             _("Step 4 — verify the speed/acceleration headroom: worst-case stress with the endstop referee, ~7 minutes. Finish with Klipper's Input Shaper panel afterwards.")),
            # row 2 — supporting actions
            ("complete", _("Save"), "color1", "CHOPPER_SAVE",
             _("Save the latest tuning result for each motor (and the extruder's last winner) into the config and restart Klipper?")),
            ("resume", _("Show"), "color2", "CHOPPER_DEMO MOTOR=AB ROUNDS=2 REPEATS=2",
             _("Play the driver defaults against the tuned registers on both motors so you can hear the difference?")),
            ("move", _("Motor A"), "color3", "CHOPPER_BELTS SHOW=A",
             _("Jog motor A (stepper_x) briefly so you can see which motor and belt it is, then release the motors?")),
            ("move", _("Motor B"), "color4", "CHOPPER_BELTS SHOW=B",
             _("Jog motor B (stepper_y) briefly so you can see which motor and belt it is, then release the motors?")),
        ]

        grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, vexpand=False)
        for index, (icon, label, style, command, confirm) in enumerate(actions):
            button = self._gtk.Button(icon, label, style)
            button.connect("clicked", self.run, command, confirm)
            grid.attach(button, index % 4, index // 4, 1, 1)
        stop = self._gtk.Button("stop", _("Stop"), "color4")
        stop.connect("clicked", self.stop)
        # 4 columns keep the buttons to three rows, leaving the status area its height
        grid.attach(stop, len(actions) % 4, len(actions) // 4, 1, 1)

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

    def tuned_registers(self, stepper):
        for driver in DRIVERS:
            section = self._printer.get_config_section(f"{driver} {stepper}")
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
    def load_state():
        try:
            with open(STATE) as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return {}
