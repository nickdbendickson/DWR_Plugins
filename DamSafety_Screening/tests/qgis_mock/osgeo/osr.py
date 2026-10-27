OAMS_TRADITIONAL_GIS_ORDER = 0


class SpatialReference:
    def ImportFromEPSG(self, code): self.code = code
    def ExportToWkt(self): return f"EPSG:{self.code}"
    # 0.17.54 (tool 16 raster_box): 'EPSG:n' strings only, as the gdal double writes them
    def ImportFromWkt(self, s): self.code = int(str(s).split(":")[-1]); return 0
    def SetFromUserInput(self, s): return self.ImportFromWkt(s)
    def SetAxisMappingStrategy(self, m): pass


class CoordinateTransformation:
    """Identity between the same EPSG code; anything else is not simulated."""
    def __init__(self, a, b):
        if a.code != b.code:
            raise NotImplementedError("osr double: EPSG:{} -> EPSG:{}".format(a.code, b.code))
    def TransformPoint(self, x, y, z=0.0): return (x, y, z)
