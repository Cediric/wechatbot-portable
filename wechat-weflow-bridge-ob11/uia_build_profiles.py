"""Built-in reviewed UIA recovery profiles; never load addresses from users.

Selection and metadata helpers are pure: no files, network, process queries or
memory operations. Adding a profile requires independent offline review of its
exact module and addresses, followed by a source change and regression tests.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import re


@dataclass(frozen=True)
class BuildProfile:
    profile_id: str
    file_version: str
    sha256: str
    architecture: str
    size_of_image: int
    read_rvas: tuple[int, ...]
    active_rva: int
    review_basis: str = "reviewed_build"

    def __post_init__(self):
        if not isinstance(self.profile_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,79}", self.profile_id) is None:
            raise ValueError("构建方案标识无效")
        if not valid_version(self.file_version) or not valid_sha256(self.sha256) or self.architecture != "AMD64":
            raise ValueError("构建方案身份字段无效")
        if self.review_basis not in ("reviewed_build", "static_equivalence"):
            raise ValueError("构建方案复核依据无效")
        if not valid_image_size(self.size_of_image) or not isinstance(self.read_rvas, tuple) or not 1 <= len(self.read_rvas) <= 8:
            raise ValueError("构建方案映像或地址列表无效")
        if (any(not isinstance(rva, int) or isinstance(rva, bool) or not 0 < rva < self.size_of_image for rva in self.read_rvas)
                or len(set(self.read_rvas)) != len(self.read_rvas)
                or not isinstance(self.active_rva, int) or isinstance(self.active_rva, bool)
                or self.active_rva not in self.read_rvas):
            raise ValueError("构建方案地址必须为映像内唯一且已复核的地址")

    def metadata(self):
        return {"profile_id": self.profile_id, "file_version": self.file_version,
                "sha256": self.sha256, "architecture": self.architecture,
                "size_of_image": self.size_of_image, "read_rvas": list(self.read_rvas),
                "active_rva": self.active_rva, "review_basis": self.review_basis}

    def matches(self, identity):
        return (isinstance(identity, Mapping) and identity.get("file_version") == self.file_version
                and identity.get("sha256") == self.sha256
                and identity.get("architecture") == self.architecture
                and _image_size(identity) == self.size_of_image)


def valid_version(value):
    return (isinstance(value, str) and len(value) <= 23
            and re.fullmatch(r"\d{1,5}(?:\.\d{1,5}){3}", value, flags=re.ASCII) is not None
            and all(int(part) <= 65535 for part in value.split(".")))


def valid_sha256(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def valid_image_size(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 0xFFFFFFFF


def _image_size(identity):
    # Real module observations use module_size; disk evidence uses SizeOfImage.
    if "module_size" in identity and "size_of_image" in identity and identity["module_size"] != identity["size_of_image"]:
        return None
    return identity.get("module_size") if "module_size" in identity else identity.get("size_of_image")


LEGACY_PROFILE = BuildProfile(
    profile_id="wechat-4.1.13.65-8f7406a8", file_version="4.1.13.65",
    sha256="8f7406a8a465e851ee10eaecccb572d0e5b4e00d480c770938c714c396097b74",
    architecture="AMD64", size_of_image=198553600,
    read_rvas=(0x0AE2B0C8, 0x0B5F7BD8), active_rva=0x0AE2B0C8,
)
# Independently reviewed complete-function static equivalence with the legacy
# gate/getter/setter. These entries do not claim runtime UIA availability.
# Only the proven primary byte is included; secondary pattern hits are excluded.
WECHAT_4_1_15_13 = BuildProfile(
    profile_id="wechat-4.1.15.13-amd64", file_version="4.1.15.13",
    sha256="10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5",
    architecture="AMD64", size_of_image=202039296,
    read_rvas=(0x0B135C38,), active_rva=0x0B135C38, review_basis="static_equivalence",
)
WECHAT_4_1_15_50 = BuildProfile(
    profile_id="wechat-4.1.15.50-amd64", file_version="4.1.15.50",
    sha256="0cf6379405432385a1c442766443450f26a7689f45e53c1b4ad2822061f3113b",
    architecture="AMD64", size_of_image=204587008,
    read_rvas=(0x0B36B9D8,), active_rva=0x0B36B9D8, review_basis="static_equivalence",
)
WECHAT_4_1_15_56 = BuildProfile(
    profile_id="wechat-4.1.15.56-amd64", file_version="4.1.15.56",
    sha256="e0428a7b691853de496532523c2c09aa095f90941c37c9f4cfb3f137f3c66556",
    architecture="AMD64", size_of_image=204795904,
    read_rvas=(0x0B39C9D8,), active_rva=0x0B39C9D8, review_basis="static_equivalence",
)
REVIEWED_PROFILES = (LEGACY_PROFILE, WECHAT_4_1_15_13, WECHAT_4_1_15_50, WECHAT_4_1_15_56)


class ProfileSelectionError(ValueError):
    def __init__(self, code):
        super().__init__("微信模块未匹配已复核的恢复方案")
        self.code = code


def supported_builds():
    return [profile.metadata() for profile in REVIEWED_PROFILES]


def select_profile(identity):
    """Select one exact reviewed identity; unknown versions/hashes stay closed."""
    identity = identity if isinstance(identity, Mapping) else {}
    if not valid_version(identity.get("file_version")):
        raise ProfileSelectionError("version_unavailable")
    candidates = [profile for profile in REVIEWED_PROFILES if profile.file_version == identity.get("file_version")]
    if not candidates:
        raise ProfileSelectionError("version_mismatch")
    if not valid_sha256(identity.get("sha256")):
        raise ProfileSelectionError("sha256_unavailable")
    candidates = [profile for profile in candidates if profile.sha256 == identity.get("sha256")]
    if not candidates:
        raise ProfileSelectionError("sha256_mismatch")
    architecture = identity.get("architecture")
    if architecture in (None, "", "unknown"):
        raise ProfileSelectionError("architecture_unavailable")
    candidates = [profile for profile in candidates if profile.architecture == architecture]
    if not candidates:
        raise ProfileSelectionError("architecture_mismatch")
    if not valid_image_size(_image_size(identity)):
        raise ProfileSelectionError("image_size_unavailable")
    candidates = [profile for profile in candidates if profile.size_of_image == _image_size(identity)]
    if not candidates:
        raise ProfileSelectionError("image_size_mismatch")
    if len(candidates) != 1:
        raise ProfileSelectionError("ambiguous_profile")
    return candidates[0]
