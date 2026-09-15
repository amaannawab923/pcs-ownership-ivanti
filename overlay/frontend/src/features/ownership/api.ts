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

import { SupersetClient } from '@superset-ui/core';
import { t } from '@apache-superset/core/translation';
import type {
  AssetType,
  OwnershipDetail,
  OwnershipListItem,
  OwnershipOwnerAssignment,
  OwnershipSubjectSearch,
  OwnershipVisibility,
} from './types';

// Every call here talks to the real backend and lets its failures surface.
//
// These functions used to fall back to mock data on any error, including the
// writes: a failed Apply resolved successfully, the drawer closed, and the
// row showed the visibility the user had asked for rather than the one the
// object actually has. A sharing control that reports success it did not get
// is worse than one that is plainly unavailable, so failures now reject and
// the caller shows them.

// Endpoints are named consistently for every asset type: the list endpoint
// is the plural of the asset type (dashboards, charts, ...) and the detail
// endpoints are namespaced under the singular form.
function listEndpoint(assetType: AssetType): string {
  return `/api/v1/ownership/${assetType}s`;
}

function detailEndpoint(assetType: AssetType, objectId: number): string {
  return `/api/v1/ownership/${assetType}/${objectId}`;
}

// What a row shows when its ownership state could not be read: nothing
// asserted, and no control offered. Distinct from a real row whose owner is
// null, which is a fact the backend reported.
export function unknownOwnership(objectId: number): OwnershipListItem {
  return {
    object_id: objectId,
    owner: null,
    visibility: null,
    unowned: false,
    can_manage: false,
    can_share: false,
  };
}

// F-1 (test-cases/ivanti-acceptance-run.md section 4): the list's Sharing
// action is disabled whenever `can_manage` is false, but that is false for
// two very different reasons -- a real non-manager, or a row whose ownership
// state could not be read at all (the list endpoint failed, or a page of it
// came back short and this row was backfilled by `unknownOwnership`). The
// second case already renders the Owner/Sharing columns as "Unknown" via
// `visibility === null` (see OwnerCell/VisibilityTag); reused here so the
// tooltip tells the two apart instead of reporting an outage as a
// permissions problem. `disabled` (the feature parked for this object) is
// its own, deliberate state and keeps the permission-denied wording.
export function sharingActionTooltip(ownership: OwnershipListItem): string {
  if (ownership.can_manage) {
    return t('Sharing');
  }
  if (ownership.visibility == null && !ownership.disabled) {
    return t(
      'Sharing is temporarily unavailable: the authorization store cannot be reached.',
    );
  }
  return t('You do not manage this object, so it cannot be shared from here.');
}

// The list endpoint reports can_manage / can_share / unowned per row, so one
// request covers a page of rows rather than one request per row.
//
// It PAGINATES, and this map has to be complete: a row missing from it renders
// as owner "Unknown" with no sharing control, which is indistinguishable from
// the endpoint being down. Taking the server default silently dropped every
// object past the first hundred -- three charts on the development stack, and
// the whole column at any real size. So follow the pages to the end.
const LIST_PAGE_SIZE = 1000;
const LIST_PAGE_LIMIT = 100; // stop rather than loop forever on a bad `count`

export async function fetchOwnershipList(
  assetType: AssetType,
): Promise<Record<number, OwnershipListItem>> {
  const byId: Record<number, OwnershipListItem> = {};
  let offset = 0;

  for (let page = 0; page < LIST_PAGE_LIMIT; page += 1) {
    // eslint-disable-next-line no-await-in-loop
    const { json } = await SupersetClient.get({
      endpoint: `${listEndpoint(assetType)}?limit=${LIST_PAGE_SIZE}&offset=${offset}`,
    });
    const result: OwnershipListItem[] = json?.result ?? [];
    result.forEach(item => {
      byId[item.object_id] = item;
    });
    offset += result.length;
    const count: number = json?.count ?? result.length;
    if (result.length === 0 || offset >= count) break;
  }

  return byId;
}

export async function fetchOwnership(
  assetType: AssetType,
  objectId: number,
): Promise<OwnershipDetail> {
  const { json } = await SupersetClient.get({
    endpoint: detailEndpoint(assetType, objectId),
  });
  return json as OwnershipDetail;
}

export async function updateVisibility(
  assetType: AssetType,
  objectId: number,
  visibility: OwnershipVisibility,
): Promise<void> {
  await SupersetClient.put({
    endpoint: `${detailEndpoint(assetType, objectId)}/visibility`,
    jsonPayload: { visibility },
  });
}

export async function addShare(
  assetType: AssetType,
  objectId: number,
  subject: string,
  role: string,
): Promise<void> {
  await SupersetClient.post({
    endpoint: `${detailEndpoint(assetType, objectId)}/shares`,
    jsonPayload: { subject, role },
  });
}

export async function removeShare(
  assetType: AssetType,
  objectId: number,
  subject: string,
): Promise<void> {
  await SupersetClient.delete({
    endpoint: `${detailEndpoint(assetType, objectId)}/shares/${encodeURIComponent(
      subject,
    )}`,
  });
}

// Assign the current user as the object's owner. The self-service path
// behind "Assign owner to me": a tenant administrator can claim an unowned
// object without the client knowing the caller's member GUID.
export async function claimOwnership(
  assetType: AssetType,
  objectId: number,
): Promise<void> {
  await SupersetClient.post({
    endpoint: `${detailEndpoint(assetType, objectId)}/claim`,
  });
}

// The drawer locks every way out while this PUT is in flight, so a socket
// that never answers would otherwise leave it with no exit at all. Generous:
// the write is one row and one authorization-store call.
export const ASSIGN_OWNER_TIMEOUT_MS = 30_000;

// Assign a named member as the object's owner: the transfer path. The
// backend accepts a user subject only (a group is rejected), requires the
// caller to be the owner, a tenant administrator or an admin, and answers
// 409 for a deactivated account. Rejections carry a `message`; the caller
// shows it as-is rather than paraphrasing it. A timeout rejects with
// `statusText: 'timeout'`, and says nothing about whether the write landed.
export async function assignOwner(
  assetType: AssetType,
  objectId: number,
  subject: string,
): Promise<OwnershipOwnerAssignment> {
  const { json } = await SupersetClient.put({
    endpoint: `${detailEndpoint(assetType, objectId)}/owner`,
    jsonPayload: { subject },
    timeout: ASSIGN_OWNER_TIMEOUT_MS,
  });
  return json as OwnershipOwnerAssignment;
}

// The tenant comes back with the rows so the picker can tell "nobody matched"
// from "this caller has no tenant, so there is nobody to list".
export async function searchSubjects(
  query: string,
): Promise<OwnershipSubjectSearch> {
  const { json } = await SupersetClient.get({
    endpoint: `/api/v1/ownership/subjects?q=${encodeURIComponent(query)}`,
  });
  return {
    result: json?.result ?? [],
    tenant: json?.tenant ?? null,
    degraded: json?.degraded ?? false,
  };
}
