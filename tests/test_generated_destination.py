"""Developer pipeline ownership safety; independent of the application SDK."""
from pathlib import Path
import importlib.util,sys,tempfile,unittest
SCRIPTS=Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0,str(SCRIPTS))
try:
    spec=importlib.util.spec_from_file_location('_owned_check_script',SCRIPTS/'check.py')
    script=importlib.util.module_from_spec(spec);spec.loader.exec_module(script)
finally:sys.path.remove(str(SCRIPTS))

class GeneratedDestination(unittest.TestCase):
    def test_owned_stale_success_cleared_and_foreign_paths_preserved(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);dist=script.reset_dist(root)
            (dist/'native-success.json').write_text('stale')
            (dist/'rebuilt').mkdir();(dist/'rebuilt/old.whl').write_bytes(b'stale')
            script.reset_dist(root)
            self.assertFalse((dist/'native-success.json').exists());self.assertFalse((dist/'rebuilt').exists())
            target=root/'unrelated';target.write_bytes(b'preserve')
            (dist/'native-success.json').write_text('stale')
            (dist/'logs').mkdir();(dist/'logs/link').symlink_to(target)
            with self.assertRaises(RuntimeError):script.reset_dist(root)
            self.assertFalse((dist/'native-success.json').exists());self.assertEqual(target.read_bytes(),b'preserve')
            (dist/'logs/link').unlink();(dist/'logs').rmdir()
            (dist/'foreign.txt').write_text('preserve')
            with self.assertRaises(RuntimeError):script.reset_dist(root)
            self.assertEqual((dist/'foreign.txt').read_text(),'preserve')
    def test_destination_symlink_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);target=root/'target';target.mkdir();(target/'sentinel').write_text('preserve');(root/'dist').symlink_to(target)
            with self.assertRaises(RuntimeError):script.reset_dist(root)
            self.assertEqual((target/'sentinel').read_text(),'preserve')
