import bmesh
import bpy
import io
import math
import mathutils
from .xray_io import ChunkedWriter, PackedWriter
from .fmt_ogf import Chunks, ModelType, VertexFormat
from .utils import is_fake_bone, find_bone_real_parent


def calculate_bbox(bpy_obj):
    bb = bpy_obj.bound_box
    mn = [bb[0][0], bb[0][1], bb[0][2]]
    mx = [bb[6][0], bb[6][1], bb[6][2]]

    def expand_children_r(cc):
        for c in cc:
            b = c.bound_box
            for i in range(3):
                mn[i] = min(mn[i], b[0][i])
                mx[i] = max(mx[i], b[6][i])
            expand_children_r(c.children)

    expand_children_r(bpy_obj.children)
    return mn, mx


def calculate_bsphere(bpy_obj):
    bb = calculate_bbox(bpy_obj)
    c = (
        (bb[0][0] + bb[1][0]) / 2,
        (bb[0][1] + bb[1][1]) / 2,
        (bb[0][2] + bb[1][2]) / 2
    )
    dx = bb[0][0] - c[0]
    dy = bb[0][1] - c[1]
    dz = bb[0][2] - c[2]
    return c, math.sqrt(dx * dx + dy * dy + dz * dz)


def max_two(dic):
    k0 = None
    mx = -1
    for k in dic.keys():
        v = dic[k]
        if v > mx:
            mx = v
            k0 = k
    k1 = None
    mx = -1
    for k in dic.keys():
        v = dic[k]
        if v > mx and k != k0:
            mx = v
            k1 = k
    return {k0: dic[k0], k1: dic[k1]}


def _export_child(bpy_obj, cw):
    bm = bmesh.new()
    bm.from_object(bpy_obj, bpy.context.scene)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bpy_data = bpy.data.meshes.new('.export-ogf')
    bm.to_mesh(bpy_data)

    bbox = calculate_bbox(bpy_obj)
    bsph = calculate_bsphere(bpy_obj)
    cw.put(Chunks.HEADER, PackedWriter()
           .putf('B', 4)  # ogf version
           .putf('B', ModelType.SKELETON_GEOMDEF_ST)
           .putf('H', 0)  # shader id
           .putf('fff', *bbox[0]).putf('fff', *bbox[1])
           .putf('fff', *bsph[0]).putf('f', bsph[1]))

    m = bpy_obj.data.materials[0]
    cw.put(Chunks.TEXTURE, PackedWriter()
           .puts(m.active_texture.name)
           .puts(m.xray.eshader))

    bml_uv = bm.loops.layers.uv.active
    bml_vw = bm.verts.layers.deform.verify()
    bpy_data.calc_tangents(bml_uv.name)
    vertices = []
    indices = []
    vmap = {}
    for f in bm.faces:
        ii = []
        for li, l in enumerate(f.loops):
            dl = bpy_data.loops[f.index * 3 + li]
            uv = l[bml_uv].uv
            vtx = (l.vert.index, l.vert.co.to_tuple(), dl.normal.to_tuple(), dl.tangent.to_tuple(), dl.bitangent.normalized().to_tuple(), (uv[0], 1 - uv[1]))
            vi = vmap.get(vtx)
            if vi is None:
                vmap[vtx] = vi = len(vertices)
                vertices.append(vtx)
            ii.append(vi)
        indices.append(ii)

    vwmx = 0
    for v in bm.verts:
        vwc = len(v[bml_vw])
        if vwc > vwmx:
            vwmx = vwc

    pw = PackedWriter()
    if vwmx == 1:
        pw.putf('II', VertexFormat.FVF_1L, len(vertices))
        for v in vertices:
            vw = bm.verts[v[0]][bml_vw]
            pw.putf('fff', *v[1])
            pw.putf('fff', *v[2])
            pw.putf('fff', *v[3])
            pw.putf('fff', *v[4])
            pw.putf('ff', *v[5])
            pw.putf('I', vw.keys()[0])
    else:
        if vwmx != 2:
            print('warning: vwmx=%i' % vwmx)
        pw.putf('II', VertexFormat.FVF_2L, len(vertices))
        for v in vertices:
            vw = bm.verts[v[0]][bml_vw]
            if len(vw) > 2:
                vw = max_two(vw)
            bw = 0
            if len(vw) == 2:
                first = True
                w0 = 0
                for vgi in vw.keys():
                    pw.putf('H', vgi)
                    if first:
                        w0 = vw[vgi]
                        first = False
                    else:
                        bw = 1 - (w0 / (w0 + vw[vgi]))
            elif len(vw) == 1:
                for vgi in vw.keys():
                    pw.putf('HH', vgi, vgi)
                bw = 0
            else:
                raise Exception('oops: %i %s' % (len(vw), vw.keys()))
            pw.putf('fff', *v[1])
            pw.putf('fff', *v[2])
            pw.putf('fff', *v[3])
            pw.putf('fff', *v[4])
            pw.putf('f', bw)
            pw.putf('ff', *v[5])
    cw.put(Chunks.VERTICES, pw)

    pw = PackedWriter()
    pw.putf('I', 3 * len(indices))
    for f in indices:
        pw.putf('HHH', *f)
    cw.put(Chunks.INDICES, pw)


def _export(bpy_obj, cw):
    bbox = calculate_bbox(bpy_obj)
    bsph = calculate_bsphere(bpy_obj)
    cw.put(Chunks.HEADER, PackedWriter()
           .putf('B', 4)  # ogf version
           .putf('B', ModelType.SKELETON_ANIM)
           .putf('H', 0)  # shader id
           .putf('fff', *bbox[0]).putf('fff', *bbox[1])
           .putf('fff', *bsph[0]).putf('f', bsph[1]))

    cw.put(Chunks.S_DESC, PackedWriter()
           .puts(bpy_obj.name)
           .puts('blender')
           .putf('III', 0, 0, 0))

    bones = []

    ccw = ChunkedWriter()
    idx = 0
    for c in bpy_obj.children:
        if c.type == 'ARMATURE':
            for b in c.data.bones:
                if is_fake_bone(b):
                    continue
                bones.append((b, c))
        if c.type != 'MESH':
            continue
        mw = ChunkedWriter()
        _export_child(c, mw)
        ccw.put(idx, mw)
        idx += 1
    cw.put(Chunks.CHILDREN, ccw)

    pw = PackedWriter()
    pw.putf('I', len(bones))
    for b, _ in bones:
        b_parent = find_bone_real_parent(b)
        pw.puts(b.name)
        pw.puts(b_parent.name if b_parent else '')
        xr = b.xray
        pw.putf('fffffffff', *xr.shape.box_rot)
        pw.putf('fff', *xr.shape.box_trn)
        pw.putf('fff', *xr.shape.box_hsz)
    cw.put(Chunks.S_BONE_NAMES, pw)

    pw = PackedWriter()
    for b, o in bones:
        bp = o.pose.bones[b.name]
        xr = b.xray
        pw.putf('I', 0x1)  # version
        pw.puts(xr.gamemtl)
        pw.putf('H', int(xr.shape.type))
        pw.putf('H', xr.shape.flags)
        pw.putf('fffffffff', *xr.shape.box_rot)
        pw.putf('fff', *xr.shape.box_trn)
        pw.putf('fff', *xr.shape.box_hsz)
        pw.putf('fff', *xr.shape.sph_pos)
        pw.putf('f', xr.shape.sph_rad)
        pw.putf('fff', *xr.shape.cyl_pos)
        pw.putf('fff', *xr.shape.cyl_dir)
        pw.putf('f', xr.shape.cyl_hgh)
        pw.putf('f', xr.shape.cyl_rad)
        pw.putf('I', int(xr.ikjoint.type))
        pw.putf('ff', bp.ik_min_x, bp.ik_max_x)
        pw.putf('ff', xr.ikjoint.lim_x_spr, xr.ikjoint.lim_x_dmp)
        pw.putf('ff', bp.ik_min_y, bp.ik_max_y)
        pw.putf('ff', xr.ikjoint.lim_y_spr, xr.ikjoint.lim_y_dmp)
        pw.putf('ff', bp.ik_min_z, bp.ik_max_z)
        pw.putf('ff', xr.ikjoint.lim_z_spr, xr.ikjoint.lim_z_dmp)
        pw.putf('ff', xr.ikjoint.spring, xr.ikjoint.damping)
        pw.putf('I', xr.ikflags)
        pw.putf('ff', xr.breakf.force, xr.breakf.torque)
        pw.putf('f', xr.friction)
        tm = b.matrix_local
        b_parent = find_bone_real_parent(b)
        if b_parent:
            tm = b_parent.matrix_local.inverted() * tm
        e = tm.to_euler('ZXY')
        pw.putf('fff', e.x, e.y, e.z)
        pw.putf('fff', *tm.to_translation())
        pw.putf('ffff', xr.mass.value, *xr.mass.center)
    cw.put(Chunks.S_IKDATA, pw)

    cw.put(Chunks.S_USERDATA, PackedWriter().puts(bpy_obj.xray.userdata))
    cw.put(Chunks.S_MOTION_REFS_0, PackedWriter().puts(bpy_obj.xray.motionrefs))


def export_file(bpy_obj, fpath):
    with io.open(fpath, 'wb') as f:
        cw = ChunkedWriter()
        _export(bpy_obj, cw)
        f.write(cw.data)