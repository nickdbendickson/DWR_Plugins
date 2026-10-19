using System; using System.IO; using System.Reflection; using RasMapperLib; using RasMapperLib.EditLayers;
class M{ static int Main(string[] a){
 // a[0]=geom hdf, a[1]=mesh name, a[2]=mode seeds:"hdf" or cellsize, a[3]=points csv (if hdf)
 try{
  var geom=new RASGeometry(a[0]); var d2=geom.D2FlowArea; int fid=d2.GetFeatureByName(a[1]);
  Console.WriteLine("fid="+fid);
  Polygon perim=d2.Geometry.MeshPerimeters.Polygon(fid);
  Console.WriteLine("perim pts="+perim.Count);
  PointMs seeds;
  if(a[2]=="csv"){ seeds=new PointMs(); foreach(var l in File.ReadAllLines(a[3])){ var p=l.Split(new char[]{','}); seeds.Add(new PointM(double.Parse(p[0]),double.Parse(p[1]))); } }
  else seeds=PointGenerator.GeneratePoints(perim,float.Parse(a[2]));
  Console.WriteLine("seeds="+seeds.Count);
  var sw=System.Diagnostics.Stopwatch.StartNew();
  var comb=new PolylineFeatureLayer("bl"); int nb=0;
  foreach(var f in d2.Geometry.BreakLines.Polylines()){ if(Polyline.IsValidPolyline(f)){ comb.AddFeature(f); nb++; } }
  try{ foreach(var f in d2.Geometry.Structures.Polylines()){ if(Polyline.IsValidPolyline(f)){ comb.AddFeature(f); nb++; } } }catch(Exception){}
  Polyline bl = (nb>0 && Environment.GetEnvironmentVariable("NOBL")==null) ? comb.CopyToMultiPartPolyline() : null; Console.WriteLine("breaklines="+nb+" used="+(bl!=null));
  var mesh=new MeshFV2D(perim,seeds,bl,null,a.Length>4?double.Parse(a[4]):0.05);
  Console.WriteLine("mesh state="+mesh.MeshCompletionState+" cells="+mesh.NonVirtualCellCount+" faces="+mesh.FaceCount+" t="+sw.Elapsed.TotalSeconds);
  var layers=new System.Collections.Generic.List<FeatureLayer>(); foreach(var l in geom.Layers){ var fl=l as FeatureLayer; if(fl!=null) layers.Add(fl);} var sup=new MultiLayerReloadSuppressor(layers);
  d2.SetMeshHasBeenRecomputed(fid,true); d2.SetFeature(fid,mesh); d2.SetMeshUpToDate(fid,true); geom.Save(); sup.Dispose();
  Console.WriteLine("saved"); return 0;
 }catch(Exception e){ Console.WriteLine("FAIL "+e); return 1; }
}}
