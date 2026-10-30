"""The image build workflow (.github/workflows/build-push.yml) after the Phase 1 split (design doc 9.1, 9.6 step 5-5).

Static checks on the file only (no YAML package needed). The workflow itself runs on GitHub, and it must not be pushed
before the ECR repository `parking-guardian/inference` exists (infra data stack applied), otherwise the inference job
fails and `update-config`, which needs every build job, never runs.
"""
import re
from pathlib import Path

WORKFLOW = (Path(__file__).resolve().parent.parent.parent / ".github" / "workflows" / "build-push.yml").read_text(encoding="utf-8")


def matrix_entries():
    """{component: its lines} from the `include:` list of the build matrix."""
    block = WORKFLOW.split("matrix:", 1)[1].split("steps:", 1)[0]
    entries = {}
    for part in re.split(r"^\s+- component:", block, flags=re.MULTILINE)[1:]:
        name = part.split("\n", 1)[0].strip()
        entries[name] = part
    return entries


def test_three_images_are_built():
    assert set(matrix_entries()) == {"backend", "frontend", "inference"}


def test_backend_is_built_from_the_api_target_not_the_prototype():
    """A plain `docker build` makes the Prototype (torch inside). The API image must be the `api` target."""
    assert re.search(r"target:\s*api\b", matrix_entries()["backend"])
    assert "target: ${{ matrix.target }}" in WORKFLOW  # handed to docker/build-push-action


def test_inference_is_built_from_the_inference_target():
    assert re.search(r"target:\s*inference\b", matrix_entries()["inference"])


def test_only_inference_needs_the_yolov5_source_and_the_big_disk():
    entries = matrix_entries()
    assert re.search(r"yolov5:\s*true", entries["inference"])
    assert not re.search(r"yolov5:\s*true", entries["backend"])  # the api target never touches it
    assert not re.search(r"yolov5:\s*true", entries["frontend"])
    assert "build-contexts: ${{ matrix.yolov5 && 'yolov5=./yolov5' || '' }}" in WORKFLOW
    for step in ("Free disk space", "Checkout YOLOv5"):
        assert re.search(rf"name: {step}[^\n]*\n\s+if: matrix\.yolov5", WORKFLOW), step


def test_images_go_to_the_repository_named_after_the_component():
    assert 'repository=parking-guardian/${{ matrix.component }}' in WORKFLOW


def test_the_config_repo_tags_are_updated_for_all_three_images():
    loop = re.search(r"for image in \(([^)]*)\)", WORKFLOW).group(1)
    assert set(re.findall(r'"parking-guardian/(\w+)"', loop)) == {"backend", "frontend", "inference"}
    assert "needs: build" in WORKFLOW  # one tag commit only when every image is in ECR


def test_the_tag_edit_still_fails_loudly_when_an_image_entry_is_missing():
    assert "if n != 1:" in WORKFLOW
    assert "sys.exit" in WORKFLOW


def test_paths_that_trigger_a_build_still_cover_the_inference_code():
    assert '"backend/**"' in WORKFLOW  # inference_service/, wsgi_inference.py and the Dockerfile are under backend/
