/**
 * Licensed to the Apache Software Foundation (ASF) under one
 * or more contributor license agreements.  See the NOTICE file
 * distributed with this work for additional information
 * regarding copyright ownership.  The ASF licenses this file
 * to you under the Apache License, Version 2.0 (the
 * "License"); you may not use this file except in compliance
 * with the License.  You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing,
 * software distributed under the License is distributed on an
 * "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
 * KIND, either express or implied.  See the License for the
 * specific language governing permissions and limitations
 * under the License.
 */

export type AssetType = 'dashboard' | 'chart';

export type OwnershipVisibility = 'private' | 'shared' | 'public';

export interface OwnershipOwner {
  id: number;
  name: string;
  guid?: string;
}

// A subject and the name to show for it: what the picker hands back, and
// what the transfer flow carries. A share is one of these plus a role.
export interface SubjectRef {
  subject: string;
  name: string;
}

export interface OwnershipShare extends SubjectRef {
  role: string;
  // True when the grant exists in the authorization store but not the local
  // mirror -- still live, still revocable, surfaced so the owner can see it.
  unmirrored?: boolean;
}

export interface OwnershipListItem {
  object_id: number;
  // The backend returns `owner: null` for objects that were never assigned
  // an owner (distinct from `unowned`, which means a recorded owner exists
  // but their account has since been deactivated).
  owner: OwnershipOwner | null;
  // null means the ownership state could not be read, which the UI shows as
  // Unknown. It is not the same as `public`, and never assumed to be.
  visibility: OwnershipVisibility | null;
  // A recorded owner whose account has since been deactivated. Distinct
  // from `owner: null`, which is an object that never had one.
  unowned: boolean;
  // Whether THIS caller may change ownership or sharing on this object.
  // Server-decided: an object in another tenant reports false even to a
  // tenant administrator.
  //
  // On a LIST row this is the page's summary, resolved in bulk without the
  // per-object reads the server makes for the detail; for a holder of the
  // manage-sharing permission it can be true here and false on the detail
  // of the same object (no dataset grant, a revocation still queued). The
  // list flag gates the entry control (the row's Sharing action); the
  // drawer re-decides every control from the detail and fails closed.
  can_manage: boolean;
  can_share: boolean;
  // The feature is mid-rollback for this object: disable() has parked its
  // row. Controls are inert (can_manage/can_share are false); the object is
  // shown but not actionable until an operator runs `superset ownership
  // enable`.
  disabled?: boolean;
}

// On which ground the caller manages the object: they own it, they
// administer its tenant, they are a Superset admin, or they hold the optional
// manage-sharing permission (OWNERSHIP_MANAGE_PERMISSION on the backend; off
// unless the deployment turns it on). Reported by the detail alongside
// can_manage so that, after transferring an object away, the drawer can say
// why its controls are still live. 'owner' and 'admin' admit every action.
// 'tenant_admin' admits ownership actions only -- transfer (to a member or
// to themselves) and claim -- and the backend refuses a visibility change,
// a share and an unshare under it (403, "take ownership first"); the detail
// reports can_share false for it, and the drawer shows the sharing controls
// inert with a "Take ownership" way in. A tenant administrator who also
// holds the manage-sharing permission is admitted to sharing on that
// permission's ground and reports can_share true. Under 'manage_permission'
// the backend refuses a claim, a release, a self-share, a self-transfer and
// making the object public (403), and the drawer does not offer the claim
// button on that ground.
export type ManageReason =
  'owner' | 'tenant_admin' | 'admin' | 'manage_permission';

export interface OwnershipDetail extends OwnershipListItem {
  shares: OwnershipShare[];
  // Null (or absent, from an older backend) when the caller cannot manage
  // the object.
  manage_reason?: ManageReason | null;
  // The caller's own subject, spelled as a share to them is (`user:<guid>`),
  // so the drawer can tell which share row is the caller's. Absent from an
  // older backend. Under 'manage_permission' the backend refuses a change to
  // that row, so the drawer does not offer one.
  caller_subject?: string | null;
}

// What PUT .../owner answers with: the object and the Superset id of the
// account that now owns it (null when ownership was released).
export interface OwnershipOwnerAssignment {
  object_id: OwnershipListItem['object_id'];
  owner_user_id: OwnershipOwner['id'] | null;
}

export type SubjectKind = 'user' | 'group';

export interface OwnershipSubjectOption {
  value: string;
  text: string;
  extra: {
    /**
     * Group member count, from the authorization store. Null on the
     * directory's fast path (the additive group.tenant relation, read
     * without a per-group member count) -- see SubjectPickerPanel's
     * rowLabels, which shows "from directory" with no count in that case.
     */
    members?: number | null;
    /** False for groups that exist only in the directory, not in Superset. */
    in_superset?: boolean;
    /**
     * True for the tenant's own membership or administrator group -- the
     * whole tenant, not a third party. Under 'manage_permission' the backend
     * refuses a share to, or an unshare of, such a group (403), so the
     * drawer shows the row locked. Decided by the server, which knows the
     * configured group-id format; absent from an older backend.
     */
    structural?: boolean;
    guid?: string;
    /** Null for a member Superset has no account for. */
    email?: string | null;
    /** The Superset account id, when the member GUID is known to Superset. */
    id?: number | null;
    type: SubjectKind;
  };
}

// What GET /subjects answers with. `tenant` is the caller's tenant GUID, or
// null when the caller has none -- in which case `result` is always empty:
// the endpoint enumerates a tenant's members and never falls back to every
// Superset account.
export interface OwnershipSubjectSearch {
  result: OwnershipSubjectOption[];
  tenant: string | null;
  /**
   * True when the directory or authorization store could not answer for
   * ALL of `result` and the endpoint fell back rather than failing the
   * request outright (F-2): the list may be incomplete, or empty for a
   * reason that is not "nobody matched". SubjectPickerPanel uses this to
   * tell that apart from a real empty result.
   */
  degraded?: boolean;
}
