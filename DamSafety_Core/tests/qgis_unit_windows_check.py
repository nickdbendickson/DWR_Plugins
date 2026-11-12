"""Real-Qt check of adopt_spinbox / adopt_lineedit (Core 1.1.0, the plugins' own windows).

    QT_QPA_PLATFORM=offscreen python3 DamSafety_Core/tests/qgis_unit_windows_check.py
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))

from qgis.PyQt.QtWidgets import (QApplication, QDoubleSpinBox, QSpinBox, QLineEdit, QLabel,
                                 QFormLayout, QWidget)

app = QApplication.instance() or QApplication([])
from DamSafety_Core.ui import unit_widgets as UW                    # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else " - " + str(detail)))
    if not ok:
        FAILS.append(name)


# a double spin box built the way the windows build them, in feet
sp = QDoubleSpinBox()
sp.setRange(0.0, 50.0)
sp.setDecimals(1)
sp.setValue(2.0)
sp.setSuffix(" ft")
lab = QLabel("Termination - rise below (ft):")
check("adopted under SI", UW.adopt_spinbox(sp, "ft", label=lab, system_value="si"))
check("shows metres", sp.suffix() == " m" and abs(QDoubleSpinBox.value(sp) - 0.61) < 1e-9,
      (sp.suffix(), QDoubleSpinBox.value(sp)))
check("range in metres", abs(QDoubleSpinBox.maximum(sp) - 15.24) < 1e-9, QDoubleSpinBox.maximum(sp))
check("label relabelled", lab.text() == "Termination - rise below (m):", lab.text())
check("untouched value() is the stored 2.0 ft exactly", sp.value() == 2.0, sp.value())
QDoubleSpinBox.setValue(sp, 1.0)                     # the user types 1 m
check("typed 1 m -> 3.2808 ft", abs(sp.value() - 1 / 0.3048) < 1e-9, sp.value())
sp.setValue(10.0)                                    # the window sets 10 ft
check("setValue(10 ft) shows 3.048 m", abs(QDoubleSpinBox.value(sp) - 3.048) < 1e-9)
check("and reads back 10 ft", sp.value() == 10.0, sp.value())
check("minimum()/maximum() in ft", abs(sp.maximum() - 50.0) < 1e-9, sp.maximum())
seen = []
sp.valueChanged.connect(lambda _v: seen.append(sp.value()))
QDoubleSpinBox.setValue(sp, 2.0)
check("a slot reading value() gets ft", seen and abs(seen[-1] - 2.0 / 0.3048) < 1e-9, seen)

# an integer spin box in feet (structure to road, 50-5280 ft)
isp = QSpinBox()
isp.setRange(50, 5280)
isp.setValue(500)
isp.setSuffix(" ft")
UW.adopt_spinbox(isp, "ft", system_value="si")
check("int spin shows whole metres", QSpinBox.value(isp) == 152 and isp.suffix() == " m",
      (QSpinBox.value(isp), isp.suffix()))
check("untouched int returns 500 ft", isp.value() == 500 and isinstance(isp.value(), int))
QSpinBox.setValue(isp, 200)
check("typed 200 m -> 656 ft (int)", isp.value() == 656, isp.value())

# a line edit in feet with its form label
w = QWidget()
form = QFormLayout(w)
le = QLineEdit("1234.5")
form.addRow("Normal pool elevation (ft)", le)
UW.adopt_form_field(form, le, "ft", system_value="si")
check("line edit shows metres", QLineEdit.text(le) == "376.276", QLineEdit.text(le))
check("form label relabelled", form.labelForField(le).text() == "Normal pool elevation (m)",
      form.labelForField(le).text())
check("untouched text() is 1234.5 ft", le.text() == "1234.5", le.text())
QLineEdit.setText(le, "400")
check("typed 400 m -> 1312.33.. ft", abs(float(le.text()) - 400 / 0.3048) < 1e-9, le.text())
QLineEdit.setText(le, "")
check("empty stays empty", le.text() == "")
QLineEdit.setText(le, "abc")
check("non-number passes through", le.text() == "abc")

# US: nothing changes
us = QDoubleSpinBox()
us.setSuffix(" ft")
check("US: not adopted", not UW.adopt_spinbox(us, "ft", system_value="us") and us.suffix() == " ft")

print("FAILED: %d" % len(FAILS) if FAILS else "all ok")
sys.stdout.flush()
os._exit(1 if FAILS else 0)
