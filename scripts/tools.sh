#!/usr/bin/env bash
# Install pinned CLI tools into ./.bin, verified against sha256 sums copied from each
# project's published checksums file. Usage: scripts/tools.sh [tool ...]; tools are d2,
# actionlint, gitleaks, trivy, temporal, otelcol-contrib, promtool, kind, kubectl and helm
# (no arguments = d2, actionlint, gitleaks and trivy).
set -euo pipefail

root="$(cd "$(dirname "$0")/.." && pwd)"
bin="$root/.bin"
mkdir -p "$bin"

case "$(uname -s)" in
  Darwin) os=darwin ;;
  Linux) os=linux ;;
  *) echo "tools.sh: unsupported OS $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  arm64 | aarch64) arch=arm64 ;;
  x86_64 | amd64) arch=amd64 ;;
  *) echo "tools.sh: unsupported arch $(uname -m)" >&2; exit 1 ;;
esac
plat="$os-$arch"

D2_VERSION=0.9.0
ACTIONLINT_VERSION=1.7.12
GITLEAKS_VERSION=8.30.1
TRIVY_VERSION=0.76.1
TEMPORAL_VERSION=1.6.1
OTEL_VERSION=0.164.0
PROMETHEUS_VERSION=3.15.1
KIND_VERSION=0.33.0
KUBECTL_VERSION=1.34.11
HELM_VERSION=3.22.1

# Sets: url, sum, member (path of the binary inside the archive).
resolve() {
  local tool="$1" asset=""
  url="" sum="" member=""
  case "$tool" in
    d2)
      case "$plat" in
        darwin-arm64) asset=macos-arm64; sum=eaf6c0c143e56dd9fa97bfb6df25ea9c1ebce40245f056a0768cf1a6c15d3064 ;;
        darwin-amd64) asset=macos-amd64; sum=cad39576a480d6bb02ea142fef1726647914b0d2da51ccc9b30b660a2b1babf0 ;;
        linux-amd64) asset=linux-amd64; sum=5669ddc46b99e942cc96078f4a4e36d5e62103348f4c05179ede27802fdd87a9 ;;
        linux-arm64) asset=linux-arm64; sum=ac2c028697199479acb321db1e3d68caee9f2ba492ed73caa3cd13f3829bf913 ;;
      esac
      url="https://github.com/terrastruct/d2/releases/download/v${D2_VERSION}/d2-v${D2_VERSION}-${asset}.tar.gz"
      member="d2-v${D2_VERSION}/bin/d2"
      ;;
    actionlint)
      case "$plat" in
        darwin-arm64) sum=aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f ;;
        darwin-amd64) sum=5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644 ;;
        linux-amd64) sum=8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8 ;;
        linux-arm64) sum=325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6 ;;
      esac
      url="https://github.com/rhysd/actionlint/releases/download/v${ACTIONLINT_VERSION}/actionlint_${ACTIONLINT_VERSION}_${os}_${arch}.tar.gz"
      member="actionlint"
      ;;
    gitleaks)
      local garch="$arch"
      [ "$arch" = amd64 ] && garch=x64
      case "$plat" in
        darwin-arm64) sum=b40ab0ae55c505963e365f271a8d3846efbc170aa17f2607f13df610a9aeb6a5 ;;
        darwin-amd64) sum=dfe101a4db2255fc85120ac7f3d25e4342c3c20cf749f2c20a18081af1952709 ;;
        linux-amd64) sum=551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb ;;
        linux-arm64) sum=e4a487ee7ccd7d3a7f7ec08657610aa3606637dab924210b3aee62570fb4b080 ;;
      esac
      url="https://github.com/gitleaks/gitleaks/releases/download/v${GITLEAKS_VERSION}/gitleaks_${GITLEAKS_VERSION}_${os}_${garch}.tar.gz"
      member="gitleaks"
      ;;
    trivy)
      case "$plat" in
        darwin-arm64) asset=macOS-ARM64; sum=11a9b6cd2752011b54f01ea4bd0dbfa9f0c41dc6538d5a3f9d23955f471bf4e5 ;;
        darwin-amd64) asset=macOS-64bit; sum=7aa762eacd582052d053b62dcf972a1ab058abe39a57ccae234ed8894945b6b7 ;;
        linux-amd64) asset=Linux-64bit; sum=c7cbe3137fb8a48a17574bede3dd6beb16742d1e9772929940fc90082c39d9c0 ;;
        linux-arm64) asset=Linux-ARM64; sum=e174dff2a6dba5e3ae41f8d0537a6324405bd08132497340304c6b20e0f16a1f ;;
      esac
      url="https://github.com/aquasecurity/trivy/releases/download/v${TRIVY_VERSION}/trivy_${TRIVY_VERSION}_${asset}.tar.gz"
      member="trivy"
      ;;
    temporal)
      case "$plat" in
        darwin-amd64) sum=4f079baa51e8cd24926af892cb9885a4c5eb446024d8719a4115ec1f5a3646d6 ;;
        darwin-arm64) sum=94cf24e616808414c9acf861af58d098270307dfeb2a143f1b7cfd0c0841add2 ;;
        linux-amd64) sum=85cfd2134eff7408e9e363f71a50f6d91f5df5f72eafa9eb87102d3793904faf ;;
        linux-arm64) sum=b771abeb421fcb722bd0c0005a05d476712f08df887e596410734636f8e9399f ;;
      esac
      url="https://github.com/temporalio/cli/releases/download/v${TEMPORAL_VERSION}/temporal_cli_${TEMPORAL_VERSION}_${os}_${arch}.tar.gz"
      member=temporal
      ;;
    otelcol-contrib)
      case "$plat" in
        darwin-amd64) sum=b53ab416e9a90625c1453692f1d3a3c4eba72904424e31516866f73064b65d4f ;;
        darwin-arm64) sum=af6a0c56886e3cc36ccebe849fa167bad2bc47a97a48fd8497a0d6f93d0cf73a ;;
        linux-amd64) sum=f0c1ce9f661399beb4a58b711924716e08ea70c3802db0715fcfde3cbbada2d4 ;;
        linux-arm64) sum=8bd4a7f443405522a2175cc1a8675f9e99cc047c7181b73461efd08efbba38d3 ;;
      esac
      url="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download/v${OTEL_VERSION}/otelcol-contrib_${OTEL_VERSION}_${os}_${arch}.tar.gz"
      member=otelcol-contrib
      ;;
    promtool)
      case "$plat" in
        darwin-amd64) sum=c89fcb30045d8c394c9d93e96cbbee3aaf2448a48326b417e811eb4e528edf88 ;;
        darwin-arm64) sum=0d4d3d9837d27e4fcd0bd16d115b1b8d6732a4bf3e69107f1190d9ef653ae471 ;;
        linux-amd64) sum=6a726885328ad9efebbf36e876ce2fb6225468a71fd1e7ce3ba666d5f4d97a84 ;;
        linux-arm64) sum=ceb6d21493c37ca0b4513b147642797c68cf39a0f50a444d50e1e80c74f21b26 ;;
      esac
      url="https://github.com/prometheus/prometheus/releases/download/v${PROMETHEUS_VERSION}/prometheus-${PROMETHEUS_VERSION}.${plat}.tar.gz"
      member="prometheus-${PROMETHEUS_VERSION}.${plat}/promtool"
      ;;
    kind)
      case "$plat" in
        darwin-amd64) sum=5a99f26f57246dc9319dd294803313197a0f34d33c525b3ea8b655db5916ece0 ;;
        darwin-arm64) sum=0c8c7dbe5e23594a198b786c4bc13dacc101fa6196b0cb0b23a1ca44e61f4b4f ;;
        linux-amd64) sum=aee6151561422756b764a4ae28e7f44cda5af5a9eead3cc9985112b1de8d8e0d ;;
        linux-arm64) sum=20022bee6cfcd5086cb7234d218e3454e6090022f2a8f55d1fa7fcf42c3867a2 ;;
      esac
      url="https://github.com/kubernetes-sigs/kind/releases/download/v${KIND_VERSION}/kind-${plat}"
      member=__raw__
      ;;
    kubectl)
      case "$plat" in
        darwin-amd64) sum=49e6f1dbfef237b64dd2887a22b03bed16c6aab8883d4c7300eea4d76bfcd095 ;;
        darwin-arm64) sum=d9e0946a4446cfd66495676273aa71c64566a49e63580136eb138ef6b562c4d9 ;;
        linux-amd64) sum=8efbb9435132a190920eb65a47a8c1ecf755ad85ab57a600c9bedbab460bb7a8 ;;
        linux-arm64) sum=5b045a4712674c88a56fd98eef4285689738b7fbe8735e1b9ee3509521af5cb4 ;;
      esac
      url="https://dl.k8s.io/release/v${KUBECTL_VERSION}/bin/${os}/${arch}/kubectl"
      member=__raw__
      ;;
    helm)
      # Checksums from https://get.helm.sh/helm-v${HELM_VERSION}-<plat>.tar.gz.sha256sum
      case "$plat" in
        darwin-amd64) sum=cf850a81b908f8eef0331e6b18776da2767bcac639ca84472311dad20008495c ;;
        darwin-arm64) sum=e73da8a0b0d5193774fdff17ce6319c7436bb22ee229b45fd64f3c40368c753a ;;
        linux-amd64) sum=896cf63684a42a723d61c980369d6e588330927deb13676ca0d85006dd6da0ea ;;
        linux-arm64) sum=4c29e286b279d170fa048ddfc3e4e83dcd069ca9c52102766ba672d7149e694d ;;
      esac
      url="https://get.helm.sh/helm-v${HELM_VERSION}-${plat}.tar.gz"
      member="${plat}/helm"
      ;;
    *)
      echo "tools.sh: unknown tool '$tool'" >&2
      exit 1
      ;;
  esac
  if [ -z "$sum" ]; then
    echo "tools.sh: no pinned checksum for $tool on $plat" >&2
    exit 1
  fi
}

verify() {
  # $1 = file, $2 = expected sha256
  if command -v sha256sum >/dev/null; then
    echo "$2  $1" | sha256sum -c -
  else
    echo "$2  $1" | shasum -a 256 -c -
  fi
}

hash_of() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# Temp dir of the current download; removed on any exit, including failures under set -e.
tmp=""
trap '[ -z "$tmp" ] || rm -rf "$tmp"' EXIT

# The stamp holds "<archive sha256> <binary sha256>". On skip, the installed binary is
# re-hashed, so a replaced or corrupted .bin/<tool> is reinstalled.
install_tool() {
  local tool="$1" stamp archive
  resolve "$tool"
  stamp="$bin/.$tool.sha256"
  if [ -x "$bin/$tool" ] && [ -f "$stamp" ] &&
    [ "$(cat "$stamp")" = "$sum $(hash_of "$bin/$tool")" ]; then
    echo "tools.sh: $tool already installed (binary checksum matches), skipping"
    return
  fi
  tmp="$(mktemp -d)"
  archive="$tmp/$(basename "$url")"
  echo "tools.sh: downloading $url"
  curl -fsSL --retry 3 -o "$archive" "$url"
  if ! (cd "$tmp" && verify "$(basename "$archive")" "$sum"); then
    echo "tools.sh: checksum mismatch for $tool, aborting" >&2
    exit 1
  fi
  if [ "$member" = __raw__ ]; then
    install -m 0755 "$archive" "$bin/$tool"
  else
    tar -xzf "$archive" -C "$tmp" "$member"
    install -m 0755 "$tmp/$member" "$bin/$tool"
  fi
  echo "$sum $(hash_of "$bin/$tool")" > "$stamp"
  rm -rf "$tmp"
  tmp=""
  echo "tools.sh: installed $tool -> .bin/$tool"
}

if [ "$#" -eq 0 ]; then
  set -- d2 actionlint gitleaks trivy
fi
for t in "$@"; do
  install_tool "$t"
done
