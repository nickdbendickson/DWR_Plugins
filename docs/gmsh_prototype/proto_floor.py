"""Mesh-only prototype: current-style (near-field 100 m2 / coarse 1000 m2) vs gmsh breakline-graded mesh.
Synthetic 32 x 32 km (20 x 20 mi) domain, dam at (3000,16000)."""
import sys, time, json, numpy as np, gmsh
L=32186.9
def h_of_area(a): return np.sqrt(4*a/np.sqrt(3))   # equilateral edge for area a
x=np.linspace(3000,L-500,400); chan=np.column_stack([x,16000+1500*np.sin(x/3000)])   # meandering flow path
roads=[np.array([[12000,500],[12000,L-500]]), np.array([[500,9000],[L-500,9000]]),
       np.array([[22000,500],[26000,L-500]])]      # 2 roads + rail
culv=[(12000,16000+1500*np.sin(12000/3000)),(22000+4000*(chan[:,1].mean()-500)/(L-1000),16000)]
breach=np.array([[2800,15800],[3200,15800],[3200,16200],[2800,16200]])
def stats(tag):
    _,el,_=gmsh.model.mesh.getElements(2); n=len(el[0]) if el else 0
    q=gmsh.model.mesh.getElementQualities(el[0],"minSICN") if n else []
    _,_,nt=gmsh.model.mesh.getElements(2); 
    return n
def run(mode):
    gmsh.initialize(); gmsh.option.setNumber("General.Terminal",0); gmsh.option.setNumber("General.NumThreads",4)
    occ=gmsh.model.geo
    pts=[occ.addPoint(*p,0) for p in [(0,0),(L,0),(L,L),(0,L)]]
    ls=[occ.addLine(pts[i],pts[(i+1)%4]) for i in range(4)]
    cl=occ.addCurveLoop(ls); s=occ.addPlaneSurface([cl]); occ.synchronize()
    def emb(poly):
        closed=np.allclose(poly[0],poly[-1]); poly=poly[:-1] if closed else poly
        ids=[occ.addPoint(px,py,0) for px,py in poly]
        if closed: ids.append(ids[0])
        return [occ.addLine(ids[i],ids[i+1]) for i in range(len(ids)-1)]
    f=gmsh.model.mesh.field
    hc=h_of_area(1000); hf=h_of_area(100)
    if mode=="current":
        # ANUGA today: near-field disk (r = 5 km here) at 100 m2, rest 1000 m2; embankments as breaklines
        f.add("Ball",1); f.setNumber(1,"Radius",5000); f.setNumber(1,"XCenter",3000); f.setNumber(1,"YCenter",16000); f.setNumber(1,"VIn",hf); f.setNumber(1,"VOut",hc)
        f.setAsBackgroundMesh(1)
    else:
        curves=[]
        for line in [chan[::4]]+roads: curves.append(emb(line))
        chan_c=curves[0]; road_c=sum(curves[1:],[])
        bp=emb(np.vstack([breach,breach[:1]]))
        occ.synchronize()
        gmsh.model.mesh.embed(1,chan_c+road_c+bp,2,s)
        f.add("Distance",1); f.setNumbers(1,"CurvesList",road_c+bp); f.setNumber(1,"Sampling",50)
        f.add("Threshold",2); f.setNumber(2,"InField",1); f.setNumber(2,"SizeMin",hf); f.setNumber(2,"SizeMax",HMAX)
        f.setNumber(2,"DistMin",60); f.setNumber(2,"DistMax",1500)
        f.add("Distance",3); f.setNumbers(3,"CurvesList",chan_c); f.setNumber(3,"Sampling",20)
        f.add("Threshold",4); f.setNumber(4,"InField",3); f.setNumber(4,"SizeMin",CH); f.setNumber(4,"SizeMax",HMAX)
        f.setNumber(4,"DistMin",250); f.setNumber(4,"DistMax",2500)
        f.add("Distance",5); f.setNumbers(5,"PointsList",[occ.addPoint(cx,cy,0) for cx,cy in culv]+[occ.addPoint(3000,16000,0)]); occ.synchronize()
        f.add("Threshold",6); f.setNumber(6,"InField",5); f.setNumber(6,"SizeMin",hf); f.setNumber(6,"SizeMax",HMAX)
        f.setNumber(6,"DistMin",40); f.setNumber(6,"DistMax",600)
        f.add("Min",7); f.setNumbers(7,"FieldsList",[2,4,6]); f.setAsBackgroundMesh(7)
    for k in ("MeshSizeExtendFromBoundary","MeshSizeFromPoints","MeshSizeFromCurvature"): gmsh.option.setNumber("Mesh."+k,0)
    gmsh.option.setNumber("Mesh.Algorithm",5)
    t=time.time(); gmsh.model.mesh.generate(2); gmsh.model.mesh.optimize("Laplace2D",niter=5); dt=time.time()-t
    _,el,_=gmsh.model.mesh.getElements(2); n=len(el[0])
    q=np.array(gmsh.model.mesh.getElementQualities(el[0],"minSICN"))
    # min angle
    nt,xyz,_=gmsh.model.mesh.getNodes(); P=dict(zip(nt,xyz.reshape(-1,3)[:,:2]))
    _,_,nodes=gmsh.model.mesh.getElements(2); tri=np.array([P[i] for i in nodes[0]]).reshape(-1,3,2)
    a=np.linalg.norm(tri[:,1]-tri[:,2],axis=1);b=np.linalg.norm(tri[:,0]-tri[:,2],axis=1);c=np.linalg.norm(tri[:,0]-tri[:,1],axis=1)
    ang=np.degrees(np.arccos(np.clip((b*b+c*c-a*a)/(2*b*c),-1,1)));ang2=np.degrees(np.arccos(np.clip((a*a+c*c-b*b)/(2*a*c),-1,1)))
    mina=np.minimum(np.minimum(ang,ang2),180-ang-ang2)
    u=tri[:,1]-tri[:,0];v=tri[:,2]-tri[:,0];area=0.5*np.abs(u[:,0]*v[:,1]-u[:,1]*v[:,0])
    # inscribed-radius proxy for CFL: r = 2A/perimeter
    rin=2*area/(a+b+c)
    gmsh.finalize()
    return dict(mode=mode,triangles=n,mesh_s=round(dt,1),min_angle_deg=round(float(mina.min()),1),
                p1_angle=round(float(np.percentile(mina,1)),1),min_area_m2=round(float(area.min()),1),
                min_inradius_m=round(float(rin.min()),2))
r=[]
pass
for HMAX,CH in [(h_of_area(5000),h_of_area(100)),(h_of_area(20000),h_of_area(250))]:
    d=run("gmsh"); d.update(coarse_area_m2=round(np.sqrt(3)/4*HMAX**2), channel_area_m2=round(np.sqrt(3)/4*CH**2)); r.append(d); print(json.dumps(d),flush=True)
json.dump(r,open("results.json","w"),indent=1)
