import os
import struct
import zlib

os.makedirs('src/web/static/icons', exist_ok=True)

svg_content = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#0f172a"/>
      <stop offset="100%" stop-color="#020617"/>
    </linearGradient>
    <linearGradient id="accent" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#8b5cf6"/>
      <stop offset="50%" stop-color="#6366f1"/>
      <stop offset="100%" stop-color="#3b82f6"/>
    </linearGradient>
    <linearGradient id="gold" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#fde047"/>
      <stop offset="100%" stop-color="#eab308"/>
    </linearGradient>
  </defs>
  <rect width="512" height="512" rx="112" fill="url(#bg)" stroke="#1e293b" stroke-width="8"/>
  <path d="M256 64 L396 116 C396 268 256 392 256 392 C256 392 116 268 116 116 Z" fill="url(#accent)" opacity="0.25"/>
  <path d="M156 180 L356 180 L380 236 L132 236 Z" fill="url(#accent)" stroke="#a855f7" stroke-width="6"/>
  <rect x="132" y="236" width="248" height="140" rx="12" fill="#1e293b" stroke="url(#accent)" stroke-width="6"/>
  <circle cx="256" cy="270" r="28" fill="url(#gold)" stroke="#ca8a04" stroke-width="4"/>
  <rect x="250" y="284" width="12" height="32" rx="6" fill="url(#gold)"/>
  <polygon points="256,120 266,144 290,144 270,158 278,182 256,168 234,182 242,158 222,144 246,144" fill="url(#gold)"/>
  <circle cx="380" cy="132" r="16" fill="#22c55e" stroke="#0f172a" stroke-width="4"/>
</svg>"""

with open('src/web/static/icons/icon.svg', 'w', encoding='utf-8') as f:
    f.write(svg_content)


def create_png(width, height, output_path):
    sig = b'\x89PNG\r\n\x1a\n'
    ihdr_data = struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0)
    ihdr_crc = struct.pack('>I', zlib.crc32(b'IHDR' + ihdr_data) & 0xffffffff)
    ihdr = struct.pack('>I', len(ihdr_data)) + b'IHDR' + ihdr_data + ihdr_crc
    
    raw = bytearray()
    center_x = width // 2
    center_y = height // 2
    for y in range(height):
        raw.append(0)
        for x in range(width):
            nx = (x - center_x) / (width / 2)
            ny = (y - center_y) / (height / 2)
            dist_sq = nx * nx + ny * ny
            box_dist = max(abs(nx), abs(ny))
            if box_dist > 0.95:
                raw.extend([11, 15, 25])
            elif box_dist > 0.85:
                raw.extend([30, 41, 59])
            elif dist_sq < 0.25:
                raw.extend([234, 179, 8])
            elif abs(nx) < 0.6 and abs(ny) < 0.5:
                raw.extend([99, 102, 241])
            else:
                raw.extend([15, 23, 42])
                
    compressed = zlib.compress(bytes(raw), 9)
    idat_crc = struct.pack('>I', zlib.crc32(b'IDAT' + compressed) & 0xffffffff)
    idat = struct.pack('>I', len(compressed)) + b'IDAT' + compressed + idat_crc
    iend = struct.pack('>I', 0) + b'IEND' + struct.pack('>I', zlib.crc32(b'IEND') & 0xffffffff)
    
    with open(output_path, 'wb') as f:
        f.write(sig + ihdr + idat + iend)

create_png(192, 192, 'src/web/static/icons/icon-192.png')
create_png(512, 512, 'src/web/static/icons/icon-512.png')
create_png(192, 192, 'src/web/static/icons/apple-touch-icon.png')
create_png(32, 32, 'src/web/static/icons/favicon.ico')
print('Icon generation complete.')
