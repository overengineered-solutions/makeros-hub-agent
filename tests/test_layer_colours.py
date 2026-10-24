"""Colour-by-layer's validated slice contract and assembled volumes; synthetic geometry only."""
import importlib.util
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from makeros_hub.printers.manager import slice_spec_of

spec = importlib.util.spec_from_file_location('ps_slice', Path(__file__).resolve().parents[1] / 'slicer/ps_slice.py')
ps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ps)


def job():
    fs = [dict(type='PLA', material='PLA', colorHex=c, trayInfoIdx=None) for c in ('#FF0000', '#0000FF')]
    return dict(mesh='model.stl', model='A1 Mini', filament=fs[0], scale=0.5,
                fixes=dict(orient=False, brim=False, supports=False),
                colours=dict(filaments=fs, parts=[dict(first=0, count=1, filament=0), dict(first=1, count=1, filament=1)]))


class LayerColours(unittest.TestCase):
    def test_validates_each_material_nozzle_and_triangle_range(self):
        raw = job()
        self.assertEqual(len(ps.read_spec(raw)['colours']['filaments']), 2)
        for change in ('overlap', 'missing', 'bad_slot', 'bool', 'petg', 'mixed', 'tpu', 'bad_colour'):
            j = job()
            if change == 'overlap': j['colours']['parts'][1]['first'] = 0
            if change == 'missing': j['colours']['parts'].pop()
            if change == 'bad_slot': j['colours']['parts'][1]['filament'] = 2
            if change == 'bool': j['colours']['parts'][0]['count'] = True
            if change == 'petg': j['colours']['filaments'][1]['type'] = 'PETG'
            if change == 'mixed': j['model'] = 'P2S'; j['colours']['filaments'][1]['material'] = 'PLA-CF'
            if change == 'tpu': j['colours']['filaments'][1]['type'] = 'TPU'
            if change == 'bad_colour': j['colours']['filaments'][1]['colorHex'] = 'red'
            with self.subTest(change=change), self.assertRaises(ps.SliceError): ps.read_spec(j)
        raw['model'] = 'H2D'; raw['nozzle'] = 'right'
        with self.assertRaises(ps.SliceError): ps.read_spec(raw)
        for f, nozzle in zip(raw['colours']['filaments'], ('right', 'left')): f['nozzle'] = nozzle
        self.assertEqual([f['nozzle'] for f in ps.read_spec(raw)['colours']['filaments']], ['right', 'left'])

    def test_one_build_item_keeps_relative_positions_and_volume_filaments(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            data = bytearray(184); struct.pack_into('<I', data, 80, 2)
            struct.pack_into('<9f', data, 96, 0, 0, 0, 10, 0, 0, 0, 10, 0)
            struct.pack_into('<9f', data, 146, 20, 0, 0, 30, 0, 0, 20, 10, 0)
            (folder/'original.stl').write_bytes(data)
            ps.scale_stl(str(folder/'original.stl'), str(folder/'scaled.stl'), 0.5)
            parts = job()['colours']['parts']
            ps.colour_3mf(str(folder/'scaled.stl'), str(folder/'model.3mf'), parts)
            with zipfile.ZipFile(folder/'model.3mf') as z:
                model = ET.fromstring(z.read('3D/3dmodel.model'))
                settings = ET.fromstring(z.read('Metadata/model_settings.config'))
            ns = {'m': 'http://schemas.microsoft.com/3dmanufacturing/core/2015/02'}
            self.assertEqual(len(model.findall('m:build/m:item', ns)), 1)
            self.assertEqual(len(model.findall('.//m:component', ns)), 2)
            x = [float(v.get('x')) for v in model.findall('.//m:vertex', ns)]
            self.assertEqual(x, [0, 5, 0, 10, 15, 10])
            self.assertEqual([m.get('value') for m in settings.findall(".//part/metadata[@key='extruder']")], ['1', '2'])
            with self.assertRaises(ps.SliceError): ps.colour_3mf(str(folder/'scaled.stl'), str(folder/'bad.3mf'), parts[:1])

    def test_capability_is_explicit_in_the_image(self):
        # Empty indexes deliberately cannot provide widths, but still exercise the protocol shape.
        self.assertTrue(ps.caps({}, {})['colours'])
        self.assertEqual(ps.caps({}, {})['prepareVersion'], 2)

    def test_hub_forwards_both_choices_and_slicer_refuses_the_unsafe_combination(self):
        raw = job()
        raw['prepare'] = dict(version=1, thicken=True, moveLargeGaps=False)
        forwarded = slice_spec_of(raw)
        self.assertEqual(forwarded['colours'], raw['colours'])
        self.assertEqual(forwarded['prepare'], raw['prepare'])
        with self.assertRaisesRegex(ps.SliceError, 'Model fixes need one colour'):
            ps.read_spec({**forwarded, 'mesh': raw['mesh']})
        del forwarded['colours']
        self.assertEqual(ps.read_spec({**forwarded, 'mesh': raw['mesh']})['prepare'], raw['prepare'])


if __name__ == '__main__':
    unittest.main()
