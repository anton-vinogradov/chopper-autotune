"""KlipperScreen panel for chopper-autotune: a one-tap app to launch tuning and
the audible before/after demo from the touchscreen, and watch live progress.

Buttons send the CHOPPER_* macros over the Klippy websocket; those macros launch
the tool detached, so the screen stays responsive. Progress comes back on
display_status.message (the tool's M117) and is shown live under the buttons;
when idle the same line shows the registers currently saved for each motor.
"""
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango
from ks_includes.screen_panel import ScreenPanel

REGISTERS = ("driver_tbl", "driver_toff", "driver_hstrt", "driver_hend")


class Panel(ScreenPanel):

    def __init__(self, screen, title):
        super().__init__(screen, title or _("Chopper Autotune"))

        # the chopper is a property of the motor, so A = stepper_x, B = stepper_y on any
        # kinematics (on CoreXY those two steppers literally are motors A and B)
        self.motors = (("stepper_x", "A"), ("stepper_y", "B"))

        actions = [
            ("fine-tune", _("Tune A"), "color1", "CHOPPER_TUNE MOTOR=A",
             _("Tune motor A (stepper_x)? The printer will home and move for several minutes.")),
            ("fine-tune", _("Tune B"), "color2", "CHOPPER_TUNE MOTOR=B",
             _("Tune motor B (stepper_y)? The printer will home and move for several minutes.")),
            ("complete", _("Both + Save"), "color3", "CHOPPER_TUNE MOTOR=AB SAVE=1",
             _("Tune both motors and save the result to the printer config?\n"
               "About 20 minutes of movement; Klipper restarts at the end.")),
            ("resume", _("Demo"), "color4", "CHOPPER_DEMO MOTOR=A ROUNDS=3",
             _("Play the driver defaults against the tuned registers on motor A\n"
               "so you can hear the difference?")),
        ]

        grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True, vexpand=False)
        for index, (icon, label, style, command, confirm) in enumerate(actions):
            button = self._gtk.Button(icon, label, style)
            button.connect("clicked", self.run, command, confirm)
            grid.attach(button, index % 2, index // 2, 1, 1)
        stop = self._gtk.Button("stop", _("Stop"), "color4")
        stop.connect("clicked", self.stop)
        grid.attach(stop, 0, 2, 2, 1)

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
        if action == "notify_status_update" and data.get("display_status", {}).get("message") is not None:
            self.show_status(data["display_status"]["message"])

    def show_status(self, message):
        text = message.strip() if message else self.saved_registers()
        self.status.set_markup(f"<span size='x-large'>{GLib.markup_escape_text(text)}</span>")

    def saved_registers(self):
        saved = []
        for stepper, name in self.motors:
            section = self._printer.get_config_section(f"tmc2209 {stepper}")
            fields = " ".join(f"{reg[7:]}{section[reg]}" for reg in REGISTERS if reg in section)
            if fields:
                saved.append(f"{name}  {fields}")
        return _("Saved:  ") + "      ".join(saved) if saved else _("Not tuned yet — pick one above")
