using System; using System.Reflection; using System.IO; using System.Diagnostics;
class D{ static int Main(string[] a){
 // a[0]=RAS dir, a[1]=cmd, rest args
 AppDomain.CurrentDomain.AssemblyResolve += (s,e)=>{ var p=Path.Combine(a[0],new AssemblyName(e.Name).Name+".dll"); return File.Exists(p)?Assembly.LoadFrom(p):null; };
 var asm=Assembly.LoadFrom(Path.Combine(a[0],"RasMapperLib.dll"));
 Console.Error.WriteLine("loaded asm"); var sw=Stopwatch.StartNew();
 try{
  if(a[1]=="props"){ var t=asm.GetType("RasMapperLib.Scripting.ComputePropertyTablesCommand",true); var o=Activator.CreateInstance(t,new object[]{a[2]}); t.GetMethod("Execute").Invoke(o,new object[]{null}); }
  else if(a[1]=="complete"){ var t=asm.GetType("RasMapperLib.Scripting.CompleteGeometryCommand",true); var o=Activator.CreateInstance(t,new object[]{a[2]}); if(a.Length>3) t.GetProperty("RasmapFilename").SetValue(o,a[3]); t.GetMethod("Execute").Invoke(o,new object[]{null}); }
  else if(a[1]=="mesh"){ var t=asm.GetType("RasMapperLib.Scripting.GenerateMeshCommand",true); var o=Activator.CreateInstance(t); t.GetProperty("GeometryFilename").SetValue(o,a[2]); t.GetProperty("MeshName").SetValue(o,a[3]); t.GetProperty("CellSize").SetValue(o,float.Parse(a[4])); if(a.Length>5) t.GetProperty("PerimeterFilename").SetValue(o,a[5]); t.GetMethod("Execute").Invoke(o,new object[]{null}); }
  else if(a[1]=="assoc"){ var t=asm.GetType("RasMapperLib.Scripting.SetGeometryAssociationCommand",true); var o=Activator.CreateInstance(t); t.GetProperty("GeometryFilename").SetValue(o,a[2]); t.GetProperty("TerrainFilename").SetValue(o,a[3]); if(a.Length>4) t.GetProperty("NValueFilename").SetValue(o,a[4]); t.GetMethod("Execute").Invoke(o,new object[]{null}); }
  Console.WriteLine("OK "+a[1]+" "+sw.Elapsed.TotalSeconds+" s"); return 0;
 }catch(Exception e){ Console.WriteLine("FAIL "+e.ToString()); return 1; }
}}
