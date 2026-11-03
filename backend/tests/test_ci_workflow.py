"""The image build workflow (.github/workflows/build-push.yml): Phase 1 split (design doc 9.1, 9.6 step 5-5) plus the Phase 2
Worker image (ADR 0019, 2026-11-03: the `prototype` target, repository parking-guardian/worker).

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


def test_four_images_are_built():
    assert set(matrix_entries()) == {"backend", "frontend", "inference", "worker"}


def test_backend_is_built_from_the_api_target_not_the_prototype():
    """A plain `docker build` makes the Prototype (torch inside). The API image must be the `api` target."""
    assert re.search(r"target:\s*api\b", matrix_entries()["backend"])
    assert "target: ${{ matrix.target }}" in WORKFLOW  # handed to docker/build-push-action


def test_inference_is_built_from_the_inference_target():
    assert re.search(r"target:\s*inference\b", matrix_entries()["inference"])


def test_the_worker_is_built_from_the_prototype_target():
    """The Phase 2 worker runs the models in-process AND talks to the database, S3 and SQS: that is the `prototype` stage
    (API dependencies + torch + YOLOv5). `api` has no torch, `inference` has no database/S3/SQS packages."""
    assert re.search(r"target:\s*prototype\b", matrix_entries()["worker"])
    dockerfile = (Path(__file__).resolve().parent.parent / "Dockerfile").read_text(encoding="utf-8")
    assert re.search(r"^FROM ai-deps AS prototype\s*$", dockerfile, flags=re.MULTILINE)
    assert "requirements.txt" in dockerfile.split("AS prototype", 1)[1]   # the stage installs the API requirements (sqlalchemy, boto3 ...)
    inference_stage = dockerfile.split("AS inference", 1)[1].split("AS prototype", 1)[0]
    assert "COPY requirements.txt" not in inference_stage                 # which is why the inference image cannot be the worker


def test_only_the_ai_images_need_the_yolov5_source_and_the_big_disk():
    entries = matrix_entries()
    assert re.search(r"yolov5:\s*true", entries["inference"])
    assert re.search(r"yolov5:\s*true", entries["worker"])     # prototype copies yolov5 into /opt/yolov5 (build context "yolov5")
    assert not re.search(r"yolov5:\s*true", entries["backend"])  # the api target never touches it
    assert not re.search(r"yolov5:\s*true", entries["frontend"])
    assert "build-contexts: ${{ matrix.yolov5 && 'yolov5=./yolov5' || '' }}" in WORKFLOW
    for step in ("Free disk space", "Checkout YOLOv5"):
        assert re.search(rf"name: {step}[^\n]*\n\s+if: matrix\.yolov5", WORKFLOW), step


def test_images_go_to_the_repository_named_after_the_component():
    assert 'repository=parking-guardian/${{ matrix.component }}' in WORKFLOW


def test_the_config_repo_tags_are_updated_for_all_four_images():
    loop = re.search(r"for image in \(([^)]*)\)", WORKFLOW).group(1)
    assert set(re.findall(r'"parking-guardian/(\w+)"', loop)) == {"backend", "frontend", "inference", "worker"}
    assert "needs: build" in WORKFLOW  # one tag commit only when every image is in ECR


def test_the_tag_edit_still_fails_loudly_when_an_image_entry_is_missing():
    assert "if n != 1:" in WORKFLOW
    assert "sys.exit" in WORKFLOW


def test_paths_that_trigger_a_build_still_cover_the_inference_code():
    assert '"backend/**"' in WORKFLOW  # inference_service/, wsgi_inference.py and the Dockerfile are under backend/


# --- build context (added after the first Phase 1 CI run failed: `path "./inference" not found`) -------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
EXPECTED_CONTEXT = {"backend": "./backend", "frontend": "./frontend", "inference": "./backend",
                    "worker": "./backend"}  # inference and worker are built from backend/


def declared_context(entry_text):
    match = re.search(r"^\s+context:\s*(\S+)", entry_text, flags=re.MULTILINE)
    return match.group(1) if match else None


def test_every_component_declares_its_own_build_context():
    entries = matrix_entries()
    assert {name: declared_context(text) for name, text in entries.items()} == EXPECTED_CONTEXT


def test_inference_is_built_from_the_backend_directory_not_from_a_folder_named_after_it():
    """The context used to be `./${{ matrix.component }}`, which made the inference job look for ./inference."""
    assert "context: ./${{ matrix.component }}" not in WORKFLOW
    assert re.search(r"^\s+context:\s*\$\{\{ matrix\.context \}\}\s*$", WORKFLOW, flags=re.MULTILINE)


def test_every_declared_context_is_a_real_directory_with_a_dockerfile():
    for name, text in matrix_entries().items():
        context = declared_context(text)
        assert context is not None, name
        folder = REPO_ROOT / context
        assert folder.is_dir(), f"{name}: build context {context} does not exist in the repository"
        assert (folder / "Dockerfile").is_file(), f"{name}: no Dockerfile in {context}"


def test_the_inference_files_the_dockerfile_copies_are_inside_its_context():
    """The inference stage copies these paths; if the context were wrong the build would fail the same way."""
    context = REPO_ROOT / declared_context(matrix_entries()["inference"])
    for needed in ("requirements-inference.txt", "app", "inference_service", "wsgi_inference.py", "gunicorn_inference.conf.py"):
        assert (context / needed).exists(), needed


# --- Phase 2: the worker image reaches the config repo and ECR (P1, P2) ---------------------------------------------------

CONFIG_REPO = REPO_ROOT.parent / "parking-guardian-config"
INFRA_REPO = REPO_ROOT.parent / "parking-guardian-infra"


def test_the_commit_message_names_every_image():
    assert "backend·frontend·inference·worker" in WORKFLOW


def tag_edit_code():
    """The Python program the workflow runs in the config checkout (the heredoc between <<'PY' and PY), un-indented."""
    import textwrap
    body = WORKFLOW.split("<<'PY'\n", 1)[1].split("\n          PY", 1)[0]
    return textwrap.dedent(body)


def test_the_tag_edit_program_rewrites_exactly_the_four_images(tmp_path):
    """Run the workflow's own tag-editing code against a copy of the real kustomization.yaml."""
    import subprocess
    import sys
    source = CONFIG_REPO / "apps" / "parking-guardian" / "kustomization.yaml"
    if not source.exists():
        import pytest
        pytest.skip("config repo not next to this repository")
    (tmp_path / "kustomization.yaml").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    done = subprocess.run([sys.executable, "-X", "utf8", "-I", "-c", tag_edit_code(), "abc1234"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert done.returncode == 0, done.stderr
    text = (tmp_path / "kustomization.yaml").read_text(encoding="utf-8")
    pairs = dict(re.findall(r"- name: parking-guardian/(\w+)\n\s+newTag: (\S+)", text))
    assert pairs == {name: '"abc1234"' for name in ("backend", "frontend", "inference", "worker")}   # quoted: numeric hashes break kustomize


def test_the_tag_edit_program_fails_when_the_worker_entry_is_missing(tmp_path):
    """A kustomization.yaml without the worker entry must stop the job instead of silently skipping the worker."""
    import subprocess
    import sys
    source = CONFIG_REPO / "apps" / "parking-guardian" / "kustomization.yaml"
    if not source.exists():
        import pytest
        pytest.skip("config repo not next to this repository")
    text = re.sub(r"  - name: parking-guardian/worker\n\s+newTag: \S+\n", "", source.read_text(encoding="utf-8"))
    (tmp_path / "kustomization.yaml").write_text(text, encoding="utf-8")
    done = subprocess.run([sys.executable, "-X", "utf8", "-I", "-c", tag_edit_code(), "abc1234"], cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert done.returncode != 0 and "parking-guardian/worker" in done.stderr + done.stdout


def test_the_worker_repository_exists_in_the_infra_code():
    ecr = INFRA_REPO / "terraform" / "stacks" / "data" / "storage_ecr.tf"
    if not ecr.exists():
        import pytest
        pytest.skip("infra repo not next to this repository")
    text = ecr.read_text(encoding="utf-8")
    assert re.search(r'ecr_repositories\s*=\s*toset\(\[[^\]]*"worker"', text)
    cicd = (INFRA_REPO / "terraform" / "stacks" / "data" / "cicd.tf").read_text(encoding="utf-8")
    assert "resources = [for r in aws_ecr_repository.this : r.arn]" in cicd   # the push permission follows the repository list
