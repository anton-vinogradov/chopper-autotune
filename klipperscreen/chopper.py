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
        # kinematics (on CoreXY those two steppers literally are motors A and B)
        self.motors = (("stepper_x", "A"), ("stepper_y", "B"))

        actions = [
            ("fine-tune", _("Tune A"), "color1", "CHOPPER_TUNE MOTOR=A",
             _("Tune motor A (stepper_x)? The printer will home and move for several minutes.")),
            ("fine-tune", _("Tune B"), "color2", "CHOPPER_TUNE MOTOR=B",
             _("Tune motor B (stepper_y)? The printer will home and move for several minutes.")),
            ("fine-tune", _("Tune both"), "color3", "CHOPPER_TUNE MOTOR=AB",
             _("Tune both motors (A and B)? About 20 minutes of movement.")),
            ("complete", _("Save"), "color1", "CHOPPER_SAVE",
             _("Save the latest tuning result for each motor into the config and restart Klipper?")),
            ("resume", _("Show"), "color2", "CHOPPER_DEMO MOTOR=AB ROUNDS=2 REPEATS=2",
             _("Play the driver defaults against the tuned registers on both motors so you can hear the difference?")),
        ]

        grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, vexpand=False)
        for index, (icon, label, style, command, confirm) in enumerate(actions):
            button = self._gtk.Button(icon, label, style)
            button.connect("clicked", self.run, command, confirm)
            grid.attach(button, index % 3, index // 3, 1, 1)
        stop = self._gtk.Button("stop", _("Stop"), "color4")
        stop.connect("clicked", self.stop)
        grid.attach(stop, 2, 1, 1, 1)

        self.status = Gtk.Label(hexpand=True, vexpand=True, halign=Gtk.Align.CENTER,
                                valign=Gtk.Align.CENTER, wrap=True,
                                wrap_mode=Pango.WrapMode.WORD_CHAR)

        self.content.add(grid)
        self.content.add(self.status)
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
            self.status.set_markup(f"<span size='x-large'>{GLib.markup_escape_text(message.strip())}</span>")
        else:
            self.status.set_markup(
                f"<span font_family='monospace' size='large'>{GLib.markup_escape_text(self.register_table())}</span>")

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
