using System; using System.IO; using System.Collections.Generic; using Geospatial.GDALAssist; using RasMapperLib; using RasMapperLib.Progress; using TiffAssist; using Utility.Progress;
class T{ static int Main(string[] a){
 // a[0]=install dir, a[1]=prj, a[2]=input tif, a[3]=out folder, a[4]=name
 try{
  string I=a[0];
  GDALSetup.Initialize(Path.Combine(I,"GDAL\\bin64"),Path.Combine(I,"GDAL\\common\\data"),Path.Combine(I,"GDAL\\bin64\\gdalplugins"),Path.Combine(I,"GDAL\\common\\WMS"));
  GDALSetup.EnsureInitialized();
  var md=new TiffMetadata<float>(-9999f, RoundTo.None); md.TileWidth=256; md.TileHeight=256; md.Software="HEC-RAS"; md.Artist="USACE HEC"; md.ImageDescription="HEC-RAS Terrain"; md.DataType="Elevation"; md.DataUnits="Feet"; md.CompressionUsed="Deflate";
  SharedData.SetUnitsSystem("English"); SharedData.SRSFilename=a[1];
  var src=new List<string>(); src.Add(a[2]); Directory.CreateDirectory(a[3]);
  var sw=System.Diagnostics.Stopwatch.StartNew();
  var b=new TerrainComputable(md,false,Projection.FromFile(a[1]),src,a[3],a[4],0f,false,true,false,FloatTiffWriter.TileWriteOperations.None);
  b.Initialize(new ConsoleDisplayProgress()); b.Run(ProgressReporter.ConsoleWrite()); b.Complete();
  Console.WriteLine("TERRAIN OK "+sw.Elapsed.TotalSeconds+" s"); return 0;
 }catch(Exception e){ Console.WriteLine("FAIL "+e); return 1; }
}}
