"""Dry dam with 10 ac-ft normal and 5,240 ac-ft maximum uses 5,240 at top of dam."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.storage import storage_choice_volume, ensure_top_of_dam

AF = 1233.48183754752

def test_maple_river_uses_max():
    normal = 10 * AF
    maximum = 5240 * AF
    vol, how = storage_choice_volume(normal, maximum, 0)
    assert abs(vol - maximum) < 1.0, vol
    assert how == "maximum storage"
    curve = [(0.0, 0.0), (10.0, normal)]
    out, note = ensure_top_of_dam(curve, maximum)
    assert abs(out[-1][1] - maximum) < 1.0
    assert "maximum" in note
    # step 5 and the Runner hand-off read the curve top, which is now the maximum
    assert out[-1][1] / AF > 5239

if __name__ == "__main__":
    test_maple_river_uses_max()
    print("ok")
