"""Role-based access control. Roles come from LDAP groups (via Keycloak 'groups' claim) or local assignment."""
from .config import ROLE_IDS, GROUP_TO_ROLE, ROLES

P_ALL = "*"
PERMISSIONS = {
    "hub-admin":       {P_ALL},
    "image-publisher": {"workspace.own", "catalog.manage", "images.view", "health.view"},
    "project-owner":   {"workspace.own", "projects.manage", "projects.view_workspaces", "finops.view"},
    "engineer":        {"workspace.own"},
    "auditor":         {"workspace.view_all", "users.view", "audit.view", "images.view", "quotas.view",
                        "health.view", "finops.view", "projects.view_workspaces"},
    "guest":           {"workspace.own"},
}
CODER_SITE_ROLES = {r["id"]: r["coder_roles"] for r in ROLES["roles"]}
DESCRIPTIONS = {r["id"]: r["description"] for r in ROLES["roles"]}

def roles_from_groups(groups):
    out = []
    for g in groups or []:
        g = g.strip("/").split("/")[-1]
        if g in GROUP_TO_ROLE and GROUP_TO_ROLE[g] not in out: out.append(GROUP_TO_ROLE[g])
        elif g in ROLE_IDS and g not in out: out.append(g)
    return out

def perms(roles):
    s = set()
    for r in roles or []: s |= PERMISSIONS.get(r, set())
    return s

def can(user, perm):
    if not user: return False
    p = perms(user.get("roles"))
    return P_ALL in p or perm in p

def coder_roles_for(roles):
    out = set()
    for r in roles or []: out |= set(CODER_SITE_ROLES.get(r, []))
    return sorted(out)
