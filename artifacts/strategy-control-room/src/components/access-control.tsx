import React, { createContext, type ReactNode, useContext } from "react";

import type { AccessRole } from "@/lib/api";

const roleLevels: Record<AccessRole, number> = {
  viewer: 0,
  researcher: 1,
  operator: 2,
  admin: 3,
};

const AccessRoleContext = createContext<AccessRole>("viewer");

export function canAccess(activeRole: AccessRole, requiredRole: AccessRole): boolean {
  return roleLevels[activeRole] >= roleLevels[requiredRole];
}

export function AccessRoleProvider({ role, children }: { role: AccessRole; children: ReactNode }) {
  return <AccessRoleContext.Provider value={role}>{children}</AccessRoleContext.Provider>;
}

export function useAccessRole() {
  return useContext(AccessRoleContext);
}

export function RoleGate({
  requires,
  children,
  className = "",
}: {
  requires: AccessRole;
  children: ReactNode;
  className?: string;
}) {
  const role = useAccessRole();
  const allowed = canAccess(role, requires);

  if (!allowed) {
    return <p className={`text-xs font-medium text-slate-500 ${className}`} role="note">
      Requires {requires.charAt(0).toUpperCase() + requires.slice(1)} access
    </p>;
  }
  return <div className={className}>{children}</div>;
}