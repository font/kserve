# Confidential Model Serving E2E Tests

End-to-end tests for KServe confidential model serving on OpenShift with
CoCo (Confidential Containers) peer-pods. Tests were run on an ARO cluster
with the Trustee operator and OpenShift Sandboxed Containers operator installed.

## Prerequisites

- OpenShift cluster with CoCo peer-pods configured (`kata-remote` runtime class)
- Trustee operator deployed with KBS accessible from guest VMs
- KServe deployed with custom controller images from the `coco` branch
- `oc` CLI authenticated to the cluster
- `podman` for building container images
- Access to a container registry (e.g. `quay.io/ifont`)

## Setup

### 1. Create test namespace

```bash
oc create namespace kserve-test
```

### 2. Configure KServe for confidential serving

Ensure the `inferenceservice-config` ConfigMap has `confidentialImage` set:

```bash
oc get configmap inferenceservice-config -n kserve \
  -o jsonpath='{.data.storageInitializer}' | python3 -m json.tool
```

The output should include:

```json
{
  "confidentialImage": "quay.io/ifont/storage-initializer-confidential:latest",
  "enableConfidentialAutoDiscovery": false
}
```

If not set, patch it:

```bash
oc get configmap inferenceservice-config -n kserve -o jsonpath='{.data.storageInitializer}' > /tmp/si.json
# Edit /tmp/si.json to add confidentialImage and enableConfidentialAutoDiscovery
oc patch configmap inferenceservice-config -n kserve \
  --type merge -p "{\"data\":{\"storageInitializer\":$(cat /tmp/si.json | jq -c .)}}"
```

### 3. Set KBS resource policy to allow-all (test only)

The default resource policy requires `ear.status == "affirming"` which may fail
due to PCR mismatches. For testing, set an allow-all policy:

```bash
oc patch configmap trusteeconfig-resource-policy -n trustee-operator-system \
  --type merge -p '{"data":{"policy.rego":"package policy\n\ndefault allow = true\n"}}'
oc rollout restart deployment trustee-deployment -n trustee-operator-system
oc wait --for=condition=available deployment/trustee-deployment \
  -n trustee-operator-system --timeout=60s
```

> **WARNING**: This means resource access is NOT gated by attestation results.
> Do not use in production. See "Known Issues" below.

### 4. Update KBS image security policy

The guest VM fetches an image security policy from KBS that controls which
container images are allowed. Add entries for any images used in the pods:

```bash
NEW_POLICY=$(cat <<'EOF'
{
    "default": [{"type": "reject"}],
    "transports": {
        "docker": {
            "quay.io/ifont": [{"type": "insecureAcceptAnything"}],
            "docker.io/kserve": [{"type": "insecureAcceptAnything"}]
        }
    }
}
EOF
)
oc create secret generic trustee-image-policy -n trustee-operator-system \
  --from-literal=policy="$NEW_POLICY" --dry-run=client -o yaml | oc apply -f -
```

- `quay.io/ifont` — storage initializer and model images
- `docker.io/kserve` — sklearn server (resolved from `modelFormat: sklearn`)

No KBS restart needed; the guest fetches the policy on each request.

### 5. Generate the sklearn test model

If `sklearn-model/model.joblib` doesn't exist, generate it:

```bash
python3 -m venv confidential-test/.venv
confidential-test/.venv/bin/pip install scikit-learn joblib

confidential-test/.venv/bin/python3 << 'PYEOF'
from sklearn.linear_model import LinearRegression
import joblib
import numpy as np

X = np.array([[1], [2], [3], [4], [5]], dtype=float)
y = X.flatten()
model = LinearRegression().fit(X, y)
joblib.dump(model, "confidential-test/sklearn-model/model.joblib")
print(f"Model saved. Prediction for [6.8]: {model.predict([[6.8]])[0]}")
PYEOF
```

Expected output: `Prediction for [6.8]: 6.8`

---

## Test 1: Unencrypted OCI Modelcar on CoCo

Verifies that a standard (unencrypted) OCI model image works with CoCo peer-pods
using the KServe modelcar mechanism.

### Build and push the model image

The model image must place files under `/models/` (KServe modelcar convention).

```bash
cd confidential-test/sklearn-model
podman build -t quay.io/ifont/sklearn-model:unencrypted -f Dockerfile .
podman push quay.io/ifont/sklearn-model:unencrypted
cd -
```

The Dockerfile:

```dockerfile
FROM python:3.11-slim
RUN pip install --no-cache-dir scikit-learn joblib kserve
COPY model.joblib /models/model.joblib
```

### Deploy the InferenceService

```bash
oc apply -f confidential-test/sklearn-oci-unencrypted.yaml
```

The manifest (`sklearn-oci-unencrypted.yaml`):

```yaml
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: sklearn-oci-coco
  namespace: kserve-test
spec:
  predictor:
    runtimeClassName: kata-remote
    annotations:
      io.katacontainers.config.hypervisor.machine_type: Standard_DC2as_v5
    model:
      modelFormat:
        name: sklearn
      storageUri: "oci://quay.io/ifont/sklearn-model:unencrypted"
```

### Wait for pod to be ready

Peer-pod VM startup takes ~2 minutes.

```bash
oc wait --for=condition=ready pod \
  -l serving.kserve.io/inferenceservice=sklearn-oci-coco \
  -n kserve-test --timeout=300s
```

### Verify inference

```bash
oc run curl-test --rm -i --restart=Never --image=curlimages/curl -n kserve-test -- \
  curl -s --max-time 10 -X POST \
  "http://sklearn-oci-coco-predictor.kserve-test.svc.cluster.local:80/v1/models/sklearn-oci-coco:predict" \
  -H "Content-Type: application/json" \
  -d '{"instances": [[6.8]]}'
```

Expected response: `{"predictions":[6.8]}`

### Clean up

```bash
oc delete inferenceservice sklearn-oci-coco -n kserve-test
```

---

## Test 2: Encrypted Model via Blob Storage (S3/MinIO)

Verifies the full confidential model serving pipeline: JWE-encrypted model in S3,
decrypted inside a TEE using keys obtained via CDH and KBS.

### Step 1: Deploy MinIO

```bash
oc apply -f confidential-test/minio.yaml
oc wait --for=condition=available deployment/minio -n kserve-test --timeout=120s
```

### Step 2: Create S3 credentials and service account

```bash
oc apply -f confidential-test/s3-secret.yaml
```

The manifest (`s3-secret.yaml`):

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: s3-credentials
  namespace: kserve-test
  annotations:
    serving.kserve.io/s3-endpoint: minio.kserve-test.svc.cluster.local:9000
    serving.kserve.io/s3-usehttps: "0"
    serving.kserve.io/s3-region: us-east-1
type: Opaque
stringData:
  AWS_ACCESS_KEY_ID: minioadmin
  AWS_SECRET_ACCESS_KEY: minioadmin
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: sa-s3
  namespace: kserve-test
secrets:
- name: s3-credentials
```

### Step 3: Generate encryption key and encrypt model

Install `jwcrypto` in a virtualenv (skip if already done in Setup step 5):

```bash
confidential-test/.venv/bin/pip install jwcrypto
```

Generate a 32-byte AES key and encrypt the model as JWE (A256KW + A256GCM):

```bash
confidential-test/.venv/bin/python3 << 'PYEOF'
import os
from pathlib import Path
from jwcrypto import jwe, jwk

# Generate 32-byte symmetric key
key_bytes = os.urandom(32)
key_path = Path("confidential-test/model-key.bin")
key_path.write_bytes(key_bytes)
print(f"Key saved to {key_path} ({len(key_bytes)} bytes)")

# Read plaintext model
model_path = Path("confidential-test/sklearn-model/model.joblib")
plaintext = model_path.read_bytes()
print(f"Model size: {len(plaintext)} bytes")

# Encrypt with JWE Compact Serialization
symmetric_key = jwk.JWK(kty="oct", k=jwk.base64url_encode(key_bytes))
token = jwe.JWE(plaintext, protected={"alg": "A256KW", "enc": "A256GCM"})
token.add_recipient(symmetric_key)
jwe_compact = token.serialize(compact=True)

# Save encrypted model
encrypted_path = Path("confidential-test/sklearn-model/model.joblib.jwe")
encrypted_path.write_text(jwe_compact)
print(f"Encrypted model saved to {encrypted_path} ({len(jwe_compact)} bytes)")

# Verify decryption round-trip
token2 = jwe.JWE()
token2.deserialize(jwe_compact)
token2.decrypt(symmetric_key)
assert token2.payload == plaintext
print("Round-trip verification: OK")
PYEOF
```

This produces:
- `model-key.bin` — 32-byte AES-256 symmetric key
- `sklearn-model/model.joblib.jwe` — JWE Compact Serialization (5-part base64url dot-separated token)

### Step 4: Upload encrypted model to MinIO

Use the MinIO client (`mc`) inside a pod to create the bucket and upload the file.
The encrypted model is passed in via a ConfigMap (it's small enough):

```bash
oc create configmap encrypted-model -n kserve-test \
  --from-file=model.joblib.jwe=confidential-test/sklearn-model/model.joblib.jwe

oc run mc-upload --rm -i --restart=Never --image=quay.io/minio/mc:latest -n kserve-test \
  --overrides='{
    "spec": {
      "containers": [{
        "name": "mc-upload",
        "image": "quay.io/minio/mc:latest",
        "command": ["sh", "-c", "mc alias set myminio http://minio.kserve-test.svc.cluster.local:9000 minioadmin minioadmin && mc mb myminio/models && mc cp /data/model.joblib.jwe myminio/models/sklearn/model.joblib.jwe && mc ls myminio/models/sklearn/"],
        "volumeMounts": [{"name": "data", "mountPath": "/data"}]
      }],
      "volumes": [{
        "name": "data",
        "configMap": {"name": "encrypted-model"}
      }]
    }
  }'
```

Expected output includes: `model.joblib.jwe` listed in `myminio/models/sklearn/`.

### Step 5: Store decryption key in KBS

Add the key to the Trustee resource secret so CDH can retrieve it via KBS
at the resource path `default/kbsres1/model-key`:

```bash
MODEL_KEY_B64=$(base64 -w0 confidential-test/model-key.bin)
oc patch secret kbsres1 -n trustee-operator-system \
  --type merge -p "{\"data\":{\"model-key\":\"$MODEL_KEY_B64\"}}"
```

### Step 6: Deploy the confidential InferenceService

```bash
oc apply -f confidential-test/sklearn-s3-confidential.yaml
```

The manifest (`sklearn-s3-confidential.yaml`):

```yaml
apiVersion: serving.kserve.io/v1beta1
kind: InferenceService
metadata:
  name: sklearn-s3-confidential
  namespace: kserve-test
spec:
  predictor:
    serviceAccountName: sa-s3
    runtimeClassName: kata-remote
    annotations:
      io.katacontainers.config.hypervisor.machine_type: Standard_DC2as_v5
    model:
      modelFormat:
        name: sklearn
      storageUri: "s3://models/sklearn"
      confidential:
        enabled: true
        resourceId: "kbs:///default/kbsres1/model-key"
```

### Step 7: Verify webhook plumbing

Confirm the webhook swapped the init container image and injected env vars:

```bash
# Check init container image
oc get pod -n kserve-test \
  -l serving.kserve.io/inferenceservice=sklearn-s3-confidential \
  -o jsonpath='{.items[0].spec.initContainers[?(@.name=="storage-initializer")].image}'
# Expected: quay.io/ifont/storage-initializer-confidential:latest

# Check env vars
oc get pod -n kserve-test \
  -l serving.kserve.io/inferenceservice=sklearn-s3-confidential \
  -o jsonpath='{range .items[0].spec.initContainers[?(@.name=="storage-initializer")].env[*]}{.name}={.value}{"\n"}{end}'
# Expected to include:
#   CONFIDENTIAL_ENABLED=true
#   CONFIDENTIAL_RESOURCE_ID=kbs:///default/kbsres1/model-key
```

### Step 8: Wait for pod to be ready

```bash
oc wait --for=condition=ready pod \
  -l serving.kserve.io/inferenceservice=sklearn-s3-confidential \
  -n kserve-test --timeout=300s
```

### Step 9: Verify inference

```bash
oc run curl-s3 --rm -i --restart=Never --image=curlimages/curl -n kserve-test -- \
  curl -s --max-time 10 -X POST \
  "http://sklearn-s3-confidential-predictor.kserve-test.svc.cluster.local:80/v1/models/sklearn-s3-confidential:predict" \
  -H "Content-Type: application/json" \
  -d '{"instances": [[6.8]]}'
```

Expected response: `{"predictions":[6.8]}`

### Clean up

```bash
oc delete inferenceservice sklearn-s3-confidential -n kserve-test
```

---

## Test 3: LLMInferenceService Confidential Plumbing

Verifies that the LLMISVC controller correctly applies confidential configuration
(image swap + env var injection). Does not test actual LLM inference (requires GPU).

### Deploy

```bash
oc apply -f confidential-test/llmisvc-tinyllama.yaml
```

The manifest (`llmisvc-tinyllama.yaml`):

```yaml
apiVersion: serving.kserve.io/v1alpha2
kind: LLMInferenceService
metadata:
  name: confidential-tinyllama
  namespace: kserve-test
spec:
  baseRefs:
  - name: kserve-config-llm-decode-template
  model:
    uri: hf://TinyLlama/TinyLlama-1.1B-Chat-v1.0
    confidential:
      enabled: true
      resourceId: "kbs:///default/kbsres1/model-key"
```

### Verify plumbing

Wait for the pod to appear (it won't become fully ready without a GPU):

```bash
oc wait --for=jsonpath='{.status.phase}'=Pending pod \
  -l serving.kserve.io/llminferenceservice=confidential-tinyllama \
  -n kserve-test --timeout=120s
```

Check init container image and env vars:

```bash
# Check init container image was swapped
oc get pod -n kserve-test \
  -l serving.kserve.io/llminferenceservice=confidential-tinyllama \
  -o jsonpath='{.items[0].spec.initContainers[?(@.name=="storage-initializer")].image}'
# Expected: quay.io/ifont/storage-initializer-confidential:latest

# Check env vars
oc get pod -n kserve-test \
  -l serving.kserve.io/llminferenceservice=confidential-tinyllama \
  -o jsonpath='{range .items[0].spec.initContainers[?(@.name=="storage-initializer")].env[*]}{.name}={.value}{"\n"}{end}'
# Expected to include:
#   CONFIDENTIAL_ENABLED=true
#   CONFIDENTIAL_RESOURCE_ID=kbs:///default/kbsres1/model-key
```

### Clean up

```bash
oc delete llminferenceservice confidential-tinyllama -n kserve-test
```

---

## Full Teardown

Remove all test resources and infrastructure:

```bash
# Delete test workloads
oc delete inferenceservice --all -n kserve-test
oc delete llminferenceservice --all -n kserve-test

# Delete MinIO and S3 credentials
oc delete deployment minio -n kserve-test
oc delete svc minio -n kserve-test
oc delete pvc minio-pvc -n kserve-test
oc delete secret s3-credentials -n kserve-test
oc delete sa sa-s3 -n kserve-test
oc delete configmap encrypted-model -n kserve-test

# Remove model key from KBS (remove the model-key entry, keep other keys)
oc patch secret kbsres1 -n trustee-operator-system \
  --type json -p '[{"op": "remove", "path": "/data/model-key"}]'

# Restore KBS resource policy (re-enable attestation enforcement)
oc patch configmap trusteeconfig-resource-policy -n trustee-operator-system \
  --type merge -p '{"data":{"policy.rego":"package policy\n\ndefault allow = false\n\nallow {\n  input[\"submods\"][\"cpu0\"][\"ear.status\"] == \"affirming\"\n}\n"}}'
oc rollout restart deployment trustee-deployment -n trustee-operator-system

# Remove docker.io/kserve from image policy (if desired)
ORIGINAL_POLICY=$(cat <<'EOF'
{
    "default": [{"type": "reject"}],
    "transports": {
        "docker": {
            "quay.io/confidential-devhub/signed": [
                {"type": "sigstoreSigned", "keyPath": "kbs:///default/conf-devhub-signature/pub-key"}
            ],
            "quay.io/ifont": [{"type": "insecureAcceptAnything"}]
        }
    }
}
EOF
)
oc create secret generic trustee-image-policy -n trustee-operator-system \
  --from-literal=policy="$ORIGINAL_POLICY" --dry-run=client -o yaml | oc apply -f -

# Delete test namespace (optional — removes everything above in one step)
oc delete namespace kserve-test
```

---

## Known Issues

### KBS Resource Policy (allow-all workaround)

The KBS resource policy was set to `default allow = true` to unblock testing.
The original policy required `ear.status == "affirming"`, but even though
attestation passed (`AzSnpVtpm Verifier/endorsement check passed`), the EAR
status was not "affirming" — likely due to PCR mismatches on registers other
than PCR8 (PCR8 was verified correct). This means attestation completes but
resource access is NOT gated by the attestation result. The teardown section
above restores the original policy.

### Encrypted OCI Images Not Supported on OpenShift

CRI-O (used exclusively by OpenShift) cannot skip image pulls for encrypted
container images. The guest VM should handle decryption, but CRI-O tries to
pull and inspect the image on the host first, failing on encrypted layers.
This is tracked in:
- https://github.com/cri-o/cri-o/issues/8261 (RFC)
- https://github.com/cri-o/cri-o/pull/8008 (fix, still unmerged)

Bare-metal CoCo with containerd + nydus-snapshotter works because containerd
can delegate the pull to the guest via a remote snapshotter. The blob storage
path (S3 + JWE) is the recommended approach for OpenShift.

### Image Security Policy

The guest VM's image security policy (fetched from KBS at
`kbs:///default/trustee-image-policy/policy`) must allow all container images
used in the pod. By default it rejects everything. We added `docker.io/kserve`
for the sklearn serving runtime image. The policy is stored in the
`trustee-image-policy` secret in `trustee-operator-system`.

### KServe Modelcar Convention

OCI model images must place model files under `/models/` (not `/mnt/models/`).
The modelcar init container validates this with:
`[ -d /models ] && [ "$(ls -A /models)" ]`
