"""Small, offline orthographic PNG renderer for CAD feedback to the vision model."""
import math
import struct
import zlib


def dot(a, b):
    return sum(x*y for x, y in zip(a, b))


def cross(a, b):
    return (a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0])


def normal(v):
    length = math.sqrt(dot(v, v)) or 1
    return tuple(x/length for x in v)


def png(path, pixels, size):
    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind+payload) & 0xffffffff)
    scanlines = b''.join(b'\0' + pixels[y*size*3:(y+1)*size*3] for y in range(size))
    path.write_bytes(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(scanlines)) + chunk(b'IEND', b''))


def render_views(stl, output, size=256):
    raw = stl.read_bytes()
    count = struct.unpack_from('<I', raw, 80)[0]
    if count > 500000 or len(raw) < 84 + count*50:
        return
    triangles = []
    bounds = [[float('inf')]*3, [float('-inf')]*3]
    for i in range(count):
        record = struct.unpack_from('<12f', raw, 84+i*50)
        verts = [record[3:6], record[6:9], record[9:12]]
        triangles.append(verts)
        for v in verts:
            for d in range(3):
                bounds[0][d] = min(bounds[0][d], v[d])
                bounds[1][d] = max(bounds[1][d], v[d])
    center = [(bounds[0][d]+bounds[1][d])/2 for d in range(3)]
    for name, direction, up in [('isometric', (1,-1,0.85),(0,0,1)), ('top',(0,0,1),(0,1,0)), ('front',(0,-1,0),(0,0,1))]:
        forward = normal(direction)
        right = normal(cross(up, forward))
        vertical = normal(cross(forward, right))
        projected = []
        span = 0
        for tri in triangles:
            pts = []
            for v in tri:
                p = [v[d]-center[d] for d in range(3)]
                x,y,z = dot(p,right),dot(p,vertical),dot(p,forward)
                span = max(span, abs(x), abs(y))
                pts.append((x,y,z))
            projected.append(pts)
        scale = (size*0.42)/(span or 1)
        pixels = bytearray([241,243,246]*(size*size))
        depth = [float('-inf')] * (size*size)
        for pts in projected:
            a,b,c = pts
            n = normal(cross([b[k]-a[k] for k in range(3)], [c[k]-a[k] for k in range(3)]))
            shade = 0.38 + 0.62*abs(dot(n, normal((-.4,.7,1))))
            color = bytes(int(v*shade) for v in (122,190,212))
            xy = [(p[0]*scale+size/2, size/2-p[1]*scale, p[2]) for p in pts]
            ymin = max(0, math.ceil(min(p[1] for p in xy)-0.5))
            ymax = min(size-1, math.floor(max(p[1] for p in xy)-0.5))
            for y in range(ymin,ymax+1):
                intersections=[]
                sy = y + 0.5
                for j in range(3):
                    x1,y1,z1=xy[j];x2,y2,z2=xy[(j+1)%3]
                    if (y1 <= sy < y2) or (y2 <= sy < y1):
                        fraction = (sy-y1)/(y2-y1)
                        intersections.append((x1+fraction*(x2-x1),z1+fraction*(z2-z1)))
                if len(intersections)>=2:
                    left,right = min(intersections),max(intersections)
                    lo=max(0,math.ceil(left[0]-0.5));hi=min(size-1,math.floor(right[0]-0.5))
                    if right[0]-left[0] < 1e-9:
                        continue
                    slope=(right[1]-left[1])/(right[0]-left[0])
                    for x in range(lo,hi+1):
                        z = left[1]+(x+0.5-left[0])*slope
                        index=y*size+x
                        if z >= depth[index]:
                            depth[index]=z
                            pixels[index*3:index*3+3]=color
        png(output / f'view-{name}.png', pixels, size)
