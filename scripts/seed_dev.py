"""Seed a dev DB with a plan, images, a user + SSH key, and the tenant IP pool.
Run against the control plane's configured DATABASE_URL. Idempotent-ish (skips if a
user already exists). For local end-to-end testing against your Proxmox node."""

import sys
from pathlib import Path

from sqlalchemy import select

from app import ipam
from app.database import Base, SessionLocal, engine
from app.models import Image, Plan, SshKey, User


def main(pubkey_path: str):
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        if db.scalar(select(User).limit(1)):
            print("already seeded")
            return
        from app.security import hash_password

        # dev login: dev@example.com / warpyard
        user = User(
            email="dev@example.com",
            password_hash=hash_password("warpyard"),
            is_admin=True,
            max_instances=5,
            max_vcpus=8,
            max_disk_gb=160,
        )
        db.add(user)
        db.flush()
        with open(pubkey_path) as f:
            db.add(SshKey(user_id=user.id, name="dev", public_key=f.read().strip()))
        db.add_all(
            [
                Plan(
                    slug="wy-1-1",
                    name="Warpyard 1GB",
                    vcpus=1,
                    memory_mb=1024,
                    disk_gb=10,
                    transfer_gb=500,
                    net_mbps=100,
                    price_cents=400,
                ),
                Plan(
                    slug="wy-2-2",
                    name="Warpyard 2GB",
                    vcpus=2,
                    memory_mb=2048,
                    disk_gb=30,
                    transfer_gb=1000,
                    net_mbps=200,
                    price_cents=800,
                ),
                Plan(
                    slug="wy-2-4",
                    name="Warpyard 4GB",
                    vcpus=2,
                    memory_mb=4096,
                    disk_gb=60,
                    transfer_gb=2000,
                    net_mbps=300,
                    price_cents=1600,
                ),
                Plan(
                    slug="wy-4-8",
                    name="Warpyard 8GB",
                    vcpus=4,
                    memory_mb=8192,
                    disk_gb=120,
                    transfer_gb=4000,
                    net_mbps=500,
                    price_cents=3200,
                ),
                # bigger tiers — on a beefy host CPU/RAM are cheap; disk is usually the cap
                Plan(
                    slug="wy-8-16",
                    name="Warpyard 16GB",
                    vcpus=8,
                    memory_mb=16384,
                    disk_gb=160,
                    transfer_gb=6000,
                    net_mbps=1000,
                    price_cents=6400,
                ),
                Plan(
                    slug="wy-16-32",
                    name="Warpyard 32GB",
                    vcpus=16,
                    memory_mb=32768,
                    disk_gb=240,
                    transfer_gb=10000,
                    net_mbps=1000,
                    price_cents=12800,
                ),
                # OS images. Docker & push-to-deploy are add-ons (deploy/addons/setup.sh),
                # runnable on any server — not images. See docs/GAME-IMAGES.md.
                Image(
                    slug="ubuntu-24.04", name="Ubuntu 24.04 LTS", distro="ubuntu", version="24.04", template_vmid=9012
                ),
                Image(slug="debian-12", name="Debian 12", distro="debian", version="12", template_vmid=9013),
                # game servers (LinuxGSM) — see docs/GAME-IMAGES.md. default_plan pre-sizes the VM.
                Image(
                    slug="minecraft",
                    name="Minecraft (LinuxGSM)",
                    distro="ubuntu",
                    version="24.04",
                    template_vmid=9003,
                    category="game",
                    lgsm_game="mcserver",
                    ports="tcp:25565",
                    default_plan="wy-2-2",
                    guidance=(
                        "In Minecraft, go to Multiplayer → Add Server and enter {endpoint}.\n"
                        "First boot installs the server — give it a few minutes before it answers."
                    ),
                ),
                Image(
                    slug="factorio",
                    name="Factorio",
                    distro="ubuntu",
                    version="24.04",
                    template_vmid=9005,
                    category="game",
                    lgsm_game="fctrserver",
                    ports="udp:34197",
                    default_plan="wy-1-1",
                    guidance=(
                        "In Factorio: Multiplayer → Connect to address → {endpoint}.\n"
                        "First boot sets up the save — give it a minute."
                    ),
                ),
                Image(
                    slug="minecraft-bedrock",
                    name="Minecraft: Bedrock",
                    distro="ubuntu",
                    version="24.04",
                    template_vmid=9006,
                    category="game",
                    lgsm_game="mcbserver",
                    ports="udp:19132",
                    default_plan="wy-1-1",
                    guidance=(
                        "In Minecraft (Bedrock): add a server using the address and port in {endpoint}.\n"
                        "First boot installs the server — give it a few minutes."
                    ),
                ),
                Image(
                    slug="valheim",
                    name="Valheim",
                    distro="ubuntu",
                    version="24.04",
                    template_vmid=9007,
                    category="game",
                    lgsm_game="vhserver",
                    ports="udp:2456,udp:2457",
                    default_plan="wy-2-2",
                    guidance=(
                        "In Valheim: Join Game → Join IP → {endpoint}.\n"
                        "Your join password is in /home/gameserver/CREDENTIALS.txt (SSH in or open the console).\n"
                        "First boot downloads the server (a few minutes)."
                    ),
                ),
                Image(
                    slug="cs",
                    name="Counter-Strike 1.6",
                    distro="ubuntu",
                    version="24.04",
                    template_vmid=9014,
                    category="game",
                    lgsm_game="csserver",
                    ports="udp:27015",
                    default_plan="wy-1-1",
                    guidance=(
                        "In Counter-Strike 1.6: open the console (~) and type: connect {endpoint}\n"
                        "Or use the community server browser and paste {endpoint}.\n"
                        "First boot installs the server (a few minutes)."
                    ),
                ),
                Image(
                    slug="cs2",
                    name="Counter-Strike 2",
                    distro="ubuntu",
                    version="24.04",
                    template_vmid=9015,
                    category="game",
                    lgsm_game="cs2server",
                    ports="udp:27015",
                    default_plan="wy-2-4",
                    guidance=(
                        "In Counter-Strike 2: open the console (~) and type: connect {endpoint}\n"
                        "First boot downloads the full game (~30 GB) — give it 15-30 minutes before it answers.\n"
                        "Defaults to a larger-disk plan for the download."
                    ),
                ),
            ]
        )
        db.flush()
        # tenant IP pool: 10.66.0.100 .. 10.66.0.200 (low range reserved for infra/manual tests)
        n = ipam.seed_pool(db, network="10.66.0.0", gateway="10.66.0.1", first=100, last=200)
        db.commit()
        print(f"seeded: user={user.id}, plans, images, {n} IPs (10.66.0.100-200)")
    finally:
        db.close()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else str(Path.home() / ".ssh/id_ed25519.pub"))
