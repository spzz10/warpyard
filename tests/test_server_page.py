"""Render checks for the server detail page (hero + tabs) — structure, variant
differences (OS vs game), and no duplicate DOM ids (a duplicate #status-chip once
shipped as a visible second status badge)."""

import re

from app import security
from app.models import EdgeMapping, Image, Instance


def _login(client, db, user):
    user.password_hash = security.hash_password("pw12345678")
    db.commit()
    r = client.post("/login", data={"email": user.email, "password": "pw12345678"})
    assert r.status_code in (200, 303)


def _no_duplicate_ids(html: str):
    ids = re.findall(r'\bid="([^"]+)"', html)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate DOM ids: {dupes}"


def test_server_page_os_variant(client, db, seeded):
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="web1",
        hostname="web1.warpyard.test",
        status="running",
        vmid=91000,
    )
    db.add(i)
    db.commit()
    _login(client, db, seeded["user"])
    html = client.get(f"/servers/{i.id}").text
    _no_duplicate_ids(html)
    assert 'class="hero"' in html and 'role="tablist"' in html
    assert 'data-tab="addons"' in html  # OS servers get the add-ons tab
    assert html.count("status-chip") == 1  # pagehead only — no duplicate from the controls include


def test_server_page_game_variant(client, db, seeded):
    game = Image(
        slug="cs2",
        name="Counter-Strike 2",
        distro="ubuntu",
        version="24.04",
        template_vmid=9015,
        category="game",
        lgsm_game="cs2server",
        ports="udp:27015",
        guidance="connect {endpoint}",
    )
    db.add(game)
    db.flush()
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=game.id,
        label="cs",
        hostname="cs.warpyard.test",
        status="running",
        vmid=91001,
    )
    db.add(i)
    db.flush()
    db.add(EdgeMapping(instance_id=i.id, public_port=30001, target_port=27015, protocol="udp"))
    db.commit()
    _login(client, db, seeded["user"])
    html = client.get(f"/servers/{i.id}").text
    _no_duplicate_ids(html)
    assert 'data-copy="cs.warpyard.test:30001"' in html  # hero shows the connect endpoint on the server's own name
    assert 'data-tab="addons"' not in html  # game servers hide the add-ons tab
    assert "connect cs.warpyard.test:30001" in html  # guidance with endpoint substituted


def test_controls_fragment_still_carries_oob_chip(client, db, seeded):
    """The 3s poll fragment must keep the out-of-band chip (that's how the pagehead updates)."""
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="web2",
        hostname="web2.warpyard.test",
        status="running",
        vmid=91002,
    )
    db.add(i)
    db.commit()
    _login(client, db, seeded["user"])
    frag = client.get(f"/servers/{i.id}/controls").text
    assert 'hx-swap-oob="true"' in frag and "status-chip" in frag


def test_addons_pane_pushdeploy_steps(client, db, seeded):
    """Add-ons tab shows run-from-your-computer one-liners: an ssh-wrapped installer
    (no interactive SSH needed) plus the two git commands, each with the port filled in."""
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="web2",
        hostname="web2.warpyard.test",
        status="running",
        vmid=91002,
    )
    db.add(i)
    db.flush()
    db.add(EdgeMapping(instance_id=i.id, public_port=2205, target_port=22, protocol="tcp"))
    db.commit()
    _login(client, db, seeded["user"])
    html = client.get(f"/servers/{i.id}").text
    assert "ssh -p 2205 root@edge.warpyard.test" in html  # installer runs over SSH from the user's machine
    assert "bash -s pushdeploy" in html and "bash -s docker" in html
    assert "git remote add warpyard ssh://root@edge.warpyard.test:2205/srv/site.git" in html
    assert "git push warpyard main" in html


def test_addons_pane_before_provisioning(client, db, seeded):
    """No SSH forward yet (still provisioning) → placeholder text, no broken commands."""
    i = Instance(
        user_id=seeded["user"].id,
        plan_id=seeded["plan"].id,
        image_id=seeded["image"].id,
        label="web3",
        hostname="web3.warpyard.test",
        status="provisioning",
        vmid=None,
    )
    db.add(i)
    db.commit()
    _login(client, db, seeded["user"])
    html = client.get(f"/servers/{i.id}").text
    assert "finishes provisioning" in html
    assert "git remote add warpyard" not in html
