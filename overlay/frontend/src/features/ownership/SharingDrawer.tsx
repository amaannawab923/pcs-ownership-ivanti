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

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { getClientErrorObject } from '@superset-ui/core';
import { t } from '@apache-superset/core/translation';
import { styled, css, useTheme } from '@apache-superset/core/theme';
import {
  Button,
  Drawer,
  Loading,
  Radio,
  Space,
} from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import {
  addShare,
  assignOwner,
  claimOwnership,
  fetchOwnership,
  removeShare,
  updateVisibility,
} from './api';
import type {
  AssetType,
  ManageReason,
  OwnershipDetail,
  OwnershipOwnerAssignment,
  OwnershipShare,
  OwnershipSubjectOption,
  OwnershipVisibility,
  SubjectKind,
  SubjectRef,
} from './types';
import SubjectPickerPanel from './SubjectPickerPanel';

type BannerTone = 'info' | 'warning' | 'error' | 'success';

const Banner = styled.div<{ tone: BannerTone }>`
  ${({ theme, tone }) => {
    const palette: Record<BannerTone, [string, string]> = {
      info: [theme.colorInfoBg, theme.colorInfoBorder],
      warning: [theme.colorWarningBg, theme.colorWarningBorder],
      error: [theme.colorErrorBg, theme.colorErrorBorder],
      success: [theme.colorSuccessBg, theme.colorSuccessBorder],
    };
    const [background, border] = palette[tone];
    return css`
      display: flex;
      gap: ${theme.sizeUnit * 2}px;
      padding: ${theme.sizeUnit * 3}px;
      margin-bottom: ${theme.sizeUnit * 4}px;
      background: ${background};
      border: 1px solid ${border};
      border-radius: ${theme.borderRadius}px;
      color: ${theme.colorText};
      font-size: ${theme.fontSizeSM}px;
      line-height: 1.5;
    `;
  }}
`;

// The object's title at the top of a view. A heading so that focus can be
// moved onto it when the view changes; the ring is suppressed because it is
// only ever focused programmatically, never a Tab stop.
const ObjectHeading = styled.h3`
  ${({ theme }) => css`
    margin: 0 0 ${theme.sizeUnit * 2}px;
    font-size: ${theme.fontSize}px;
    font-weight: ${theme.fontWeightStrong};
    line-height: 1.5;
    &:focus {
      outline: none;
    }
  `}
`;

const OwnerRow = styled.div`
  ${({ theme }) => css`
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: ${theme.sizeUnit * 2}px;
    margin-bottom: ${theme.sizeUnit * 4}px;
    color: ${theme.colorTextSecondary};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const Consequences = styled.ul`
  ${({ theme }) => css`
    margin: ${theme.sizeUnit * 3}px 0 0;
    padding-left: ${theme.sizeUnit * 5}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit * 2}px;
    color: ${theme.colorText};
    line-height: 1.5;
  `}
`;

const AccessOption = styled.div`
  ${({ theme }) => css`
    .option-title {
      font-weight: ${theme.fontWeightStrong};
    }
    .option-description {
      color: ${theme.colorTextSecondary};
      font-size: ${theme.fontSizeSM}px;
    }
  `}
`;

const SharedSummary = styled.div`
  ${({ theme }) => css`
    margin-top: ${theme.sizeUnit * 3}px;
    margin-left: ${theme.sizeUnit * 6}px;
    display: flex;
    flex-direction: column;
    gap: ${theme.sizeUnit}px;
    color: ${theme.colorText};
    font-size: ${theme.fontSizeSM}px;
  `}
`;

const Footer = styled.div`
  ${({ theme }) => css`
    display: flex;
    justify-content: flex-end;
    gap: ${theme.sizeUnit * 2}px;
  `}
`;

// An owner is always a person; the transfer picker never offers groups.
const USERS_ONLY: SubjectKind[] = ['user'];

type DrawerView = 'main' | 'picker' | 'transfer' | 'transfer-confirm';

// What the drawer knows once the PUT has succeeded and it has tried to read
// the object back:
// - 'managed': read back with can_manage true (an administrator, say);
// - 'lost': read back with can_manage false, or refused outright -- a 403,
//   or the 404 a private object answers its previous owner with;
// - 'unread': the re-read failed for some other reason (a 5xx, a dropped
//   connection). The transfer happened; whether the caller still manages the
//   object is simply not known.
type TransferOutcome = 'managed' | 'lost' | 'unread';

interface TransferResult {
  to: string;
  outcome: TransferOutcome;
  // Why the caller still manages the object when they do, as the re-read
  // reported it. Absent from an older backend.
  reason?: ManageReason | null;
}

// getClientErrorObject spreads the Response summary into its result, but its
// type does not name `status` on every branch of the union. A request the
// client gave up on (see ASSIGN_OWNER_TIMEOUT_MS) has no status at all; it is
// told apart by the statusText the client's timeout rejects with.
async function errorDetails(
  caught: Parameters<typeof getClientErrorObject>[0],
): Promise<{ message?: string; status?: number; timedOut: boolean }> {
  const { error, status, statusText } = (await getClientErrorObject(
    caught,
  )) as {
    error?: string;
    status?: number;
    statusText?: string;
  };
  return { message: error, status, timedOut: statusText === 'timeout' };
}

// The one sentence the drawer says about a transfer that went through. When
// the caller still manages the object the controls stay live, which for the
// person who just handed it over is a surprise unless the reason is named.
function transferredMessage({ to, outcome, reason }: TransferResult): string {
  if (outcome === 'lost') {
    return t(
      'Ownership transferred to %s. You no longer manage this object, so its sharing settings can only be changed by the new owner or an administrator.',
      to,
    );
  }
  if (outcome === 'unread') {
    return t(
      'Ownership was transferred to %s; the latest details could not be loaded. Close and reopen this panel to see the current settings.',
      to,
    );
  }
  if (reason === 'tenant_admin') {
    return t(
      'Ownership transferred to %s. You still manage this object as a tenant administrator.',
      to,
    );
  }
  if (reason === 'admin') {
    return t(
      'Ownership transferred to %s. You still manage this object as an administrator.',
      to,
    );
  }
  if (reason === 'owner') {
    return t(
      'Ownership transferred to %s. You still manage this object as its owner.',
      to,
    );
  }
  if (reason === 'manage_permission') {
    // Not an owner, not an administrator: a deployment has granted a
    // separate permission to manage sharing, and the drawer names it so the
    // live controls are not mistaken for ownership. It is a sharing
    // permission: the holder can share the object and hand it to someone
    // else, but the backend refuses a claim, a self-transfer and making the
    // object public under it, so the sentence says what stays open.
    return t(
      'Ownership transferred to %s. You still manage sharing for this object through your sharing-management permission: you can share it or transfer it to someone else, but not make it public or take ownership yourself.',
      to,
    );
  }
  return t('Ownership transferred to %s. You still manage this object.', to);
}

// Lower-cased for comparison. The detail reports the owner's GUID as the
// username is spelled; the authorization store, and so /subjects, lower-cases
// it.
function normalizeGuid(guid: string | undefined): string | null {
  return guid ? guid.toLowerCase() : null;
}

function subjectKeys(shares: OwnershipShare[]): Set<string> {
  return new Set(shares.map(s => s.subject));
}

// Compares roles as well as subjects. Comparing subject keys alone meant a
// role change registered as no change: Apply stayed disabled and the diff
// below built no request for it.
function sharesEqual(a: OwnershipShare[], b: OwnershipShare[]): boolean {
  if (a.length !== b.length) return false;
  const roleOf = new Map(b.map(s => [s.subject, s.role]));
  return a.every(s => roleOf.get(s.subject) === s.role);
}

export interface SharingDrawerProps {
  assetType: AssetType;
  objectId: number;
  objectTitle: string;
  onClose: () => void;
  onApplied: () => void;
}

export default function SharingDrawer({
  assetType,
  objectId,
  objectTitle,
  onClose,
  onApplied,
}: SharingDrawerProps) {
  const theme = useTheme();
  const [loading, setLoading] = useState(true);
  const [applying, setApplying] = useState(false);
  const [detail, setDetail] = useState<OwnershipDetail | null>(null);
  // Initialized from the fetched detail below — must never default to
  // 'shared' before the real visibility is known, or the radio group will
  // briefly (or on a slow/failed fetch, persistently) show the wrong access
  // level for the object.
  const [visibility, setVisibility] = useState<OwnershipVisibility>('private');
  const [shares, setShares] = useState<OwnershipShare[]>([]);
  const [view, setView] = useState<DrawerView>('main');
  // A failed read or write is shown here and the drawer stays open. It used
  // to be swallowed: Apply resolved, the drawer closed, and the row redrew
  // with the visibility the user had asked for rather than the one the
  // object actually has.
  const [error, setError] = useState<string | null>(null);
  const [claiming, setClaiming] = useState(false);
  // The transfer flow: who was picked, whether the PUT is in flight, what the
  // API said if it refused, and -- once it went through -- who owns the object
  // now and whether this caller still manages it.
  const [transferTarget, setTransferTarget] = useState<SubjectRef | null>(null);
  const [transferring, setTransferring] = useState(false);
  const [transferError, setTransferError] = useState<string | null>(null);
  const [transferred, setTransferred] = useState<TransferResult | null>(null);
  const confirmHeadingRef = useRef<HTMLHeadingElement>(null);
  const mainHeadingRef = useRef<HTMLHeadingElement>(null);
  const previousView = useRef<DrawerView>('main');

  // Resolves to whether the read succeeded. On a failure the detail is
  // dropped, not kept: a detail the server will no longer confirm is not a
  // basis for offering any control, and a stale `can_manage: true` kept the
  // transfer link and confirm button live after the caller had lost them.
  // `failureMessage` is what the banner says; the default is for the
  // opening read, where nothing has been attempted on the object yet.
  const loadDetail = useCallback(
    (
      failureMessage: string = t(
        'Could not read this object’s sharing settings. Nothing has been changed.',
      ),
    ): Promise<boolean> => {
      setLoading(true);
      setError(null);
      return fetchOwnership(assetType, objectId)
        .then(result => {
          setDetail(result);
          if (result.visibility) setVisibility(result.visibility);
          setShares(result.shares);
          setLoading(false);
          return true;
        })
        .catch(() => {
          setDetail(null);
          setError(failureMessage);
          setLoading(false);
          return false;
        });
    },
    [assetType, objectId],
  );

  // For a re-read that follows a write, or an attempted one: whether the
  // object changed is not this sentence's to claim.
  const rereadFailedMessage = t(
    'Could not read this object’s sharing settings. Close and reopen this panel to see the current settings.',
  );

  useEffect(() => {
    // Reset any state staged for a previously opened object so a stale
    // visibility/shares value can never leak into the next fetch's render.
    setDetail(null);
    setShares([]);
    setView('main');
    setTransferTarget(null);
    setTransferError(null);
    setTransferred(null);
    loadDetail();
  }, [loadDetail]);

  // Each view replaces the drawer's content wholesale, so the control that
  // was clicked to get there is unmounted and focus falls back to the
  // document body. The picker lands on its search box itself (autoFocus);
  // the confirmation has no input, so its heading takes the focus; and the
  // way back -- Back, Cancel, or a transfer landing -- puts it on the main
  // view's heading, which is rendered whatever the detail read answered.
  // Not on the initial render: the drawer places its own focus on opening.
  useEffect(() => {
    const cameFrom = previousView.current;
    previousView.current = view;
    if (view === 'transfer-confirm') confirmHeadingRef.current?.focus();
    else if (view === 'main' && cameFrom !== 'main') {
      mainHeadingRef.current?.focus();
      // A refusal shown in the transfer view stays that view's: back on
      // the main view it would otherwise be announced as a refusal of
      // "Take ownership".
      setTransferError(null);
    }
  }, [view]);

  const handleClaim = async () => {
    setClaiming(true);
    try {
      await claimOwnership(assetType, objectId);
      await loadDetail(rereadFailedMessage);
      onApplied();
    } catch (caught) {
      setError(t('Could not assign ownership. Please try again.'));
    } finally {
      setClaiming(false);
    }
  };

  const dirty = useMemo(() => {
    if (!detail) return false;
    if (visibility !== detail.visibility) return true;
    if (visibility === 'shared' && !sharesEqual(shares, detail.shares)) {
      return true;
    }
    return false;
  }, [detail, visibility, shares]);

  // A share row the owner held before becoming the owner survives the
  // transfer (the backend leaves share tuples alone), so the owner is not
  // counted among the people the object is shared with.
  const ownerGuid = normalizeGuid(detail?.owner?.guid);
  const ownerSubject = ownerGuid ? `user:${ownerGuid}` : null;
  const userCount = shares.filter(
    s =>
      !s.subject.startsWith('group:') &&
      s.subject.toLowerCase() !== ownerSubject,
  ).length;
  const groupCount = shares.filter(s => s.subject.startsWith('group:')).length;

  // "No active owner": either never owned (owner null) or the recorded owner's
  // account is deactivated (unowned). Sharing and visibility are blocked until
  // an owner exists; a caller who can manage the object may assign one.
  const needsOwner = !!detail && (!detail.owner || !!detail.unowned);
  // The manage-sharing permission never admits a claim (POST /claim answers
  // 403 under it), so the button is not offered on its strength even if a
  // detail were to report it on an object without an active owner.
  const managesSharingOnly = detail?.manage_reason === 'manage_permission';
  // Under that permission the backend also refuses making the object public,
  // any change to the caller's OWN share, and a share to (or unshare of) the
  // tenant's own membership or administrator group -- the whole tenant, not
  // a third party (403 on Apply). None is offered: the Public option is
  // disabled with the reason beside it, and the caller's row and the
  // structural group rows in the share picker are shown locked. The subject
  // is compared case-insensitively: the detail spells it as the store does,
  // /subjects lower-cases the GUID as well, and a spelling that differs only
  // in case must not unlock the row. Which groups are structural is the
  // server's call (`extra.structural` on the /subjects row): it knows the
  // configured group-id format, and the client does not re-derive it.
  const callerSubject = detail?.caller_subject?.toLowerCase() ?? null;
  const lockedShareReason = useCallback(
    (option: OwnershipSubjectOption): string | null => {
      if (!managesSharingOnly) return null;
      if (
        callerSubject !== null &&
        option.value.toLowerCase() === callerSubject
      ) {
        return t(
          'Your own share can only be changed by the owner or an administrator.',
        );
      }
      if (option.extra?.type === 'group' && option.extra.structural) {
        return t(
          'The whole tenant can only be shared by the owner or an administrator.',
        );
      }
      return null;
    },
    [managesSharingOnly, callerSubject],
  );
  const canAssignOwner =
    !!detail && detail.can_manage && needsOwner && !managesSharingOnly;
  // A standing notice for anyone whose controls are live on an object they
  // do not own: who the owner is, and on what ground the caller may act. A
  // tenant administrator opening a member's drawer otherwise sees exactly
  // what the owner sees, and nothing tells them they are changing someone
  // else's object. Named per ground, since each allows different things.
  // Not shown while the object has no active owner (the unowned notice
  // above already says what to do) or when the caller cannot manage it.
  const managingAsNotice = useMemo((): string | null => {
    if (!detail || !detail.can_manage || needsOwner || !detail.owner) {
      return null;
    }
    // The seam reports a Superset admin's ground ahead of the owner's, so
    // an admin who owns the object would otherwise be told they are not
    // the owner -- by their own name. The owner is never "managing as".
    if (
      callerSubject !== null &&
      ownerSubject !== null &&
      callerSubject === ownerSubject.toLowerCase()
    ) {
      return null;
    }
    const owner = detail.owner.name;
    switch (detail.manage_reason) {
      case 'tenant_admin':
        return t(
          'You are not the owner of this object; %s is. As an administrator of this tenant you can transfer its ownership, but only its owner can change how it is shared. To change the sharing, take ownership first.',
          owner,
        );
      case 'admin':
        return t(
          'You are not the owner of this object; %s is. As a Superset administrator you can change who it is shared with and transfer its ownership.',
          owner,
        );
      case 'manage_permission':
        return t(
          'You are not the owner of this object; %s is. Your sharing-management permission lets you share it with others and transfer it to someone else, but not make it public or take ownership yourself.',
          owner,
        );
      default:
        return null;
    }
  }, [detail, needsOwner, callerSubject, ownerSubject]);
  // Transfer is for an object that HAS an active owner; an unowned one goes
  // through "Assign owner to me" above instead. can_manage is the server's
  // owner / tenant-administrator / admin / manage-sharing decision, and it
  // is already false for a parked object. For a manage-sharing holder this
  // is exactly the case the backend allows: a transfer to a third party on
  // an object with a live owner.
  const canTransfer = !!detail && detail.can_manage && !needsOwner;
  // After a transfer away from themselves the caller may still be able to
  // read the object (it is public, or shared with them) but no longer change
  // it. Everything that writes is inert from here on. Any outcome other than
  // a fresh detail saying can_manage is true fails closed, including the one
  // where the re-read simply did not happen, and the one where there is no
  // detail at all because the read failed.
  const readOnly =
    (!loading && !detail) ||
    (!!detail && !detail.can_manage) ||
    (!!transferred && transferred.outcome !== 'managed');
  // A tenant administrator's authority is over who OWNS the object, not
  // over who sees it: they may transfer it (to a member, or to themselves)
  // but the backend refuses a change to its visibility or shares under that
  // ground (403). So the sharing controls are shown but inert, the notice
  // says why, and "Take ownership" is the way in.
  const transferOnly =
    !!detail &&
    detail.manage_reason === 'tenant_admin' &&
    detail.can_manage &&
    !detail.can_share;
  const radiosDisabled = needsOwner || readOnly || transferOnly;
  // The current owner is left out of the transfer picker: handing the object
  // to the person who already has it is a no-op. The detail and /subjects do
  // not always spell the same account the same way -- the detail reports the
  // username, the store lower-cases its GUID, and an account with no GUID at
  // all is `local-<id>` to the store -- so the GUIDs are compared
  // case-insensitively and, failing that, the Superset id decides. A row that
  // reports an id has answered either way: `null` means the member has no
  // Superset account, and the owner always has one. Only a row from a
  // /subjects that reports no id at all falls back to the display name --
  // two people in one tenant can share a name, and a namesake of the owner
  // must not vanish from the list without a word.
  const ownerId = detail?.owner?.id;
  const ownerName = detail?.owner?.name;
  const isCurrentOwner = useCallback(
    (option: OwnershipSubjectOption) => {
      if (option.extra.type !== 'user') return false;
      const rowGuid = normalizeGuid(
        option.extra.guid ?? option.value.replace(/^user:/, ''),
      );
      if (ownerGuid && rowGuid === ownerGuid) return true;
      if (option.extra.id !== undefined) return option.extra.id === ownerId;
      return !!ownerName && option.text === ownerName;
    },
    [ownerGuid, ownerId, ownerName],
  );

  // A share row naming the owner: normally none (the picker never offers
  // the owner), but a transfer leaves the new owner's earlier share in
  // place. Such a row stays offered so it can be unticked; hiding it would
  // leave a share that is counted but cannot be removed from here. Judged
  // per row against the shares the drawer loaded (not the staged list, so
  // an untick can be reverted before Apply), on the subject spelling the
  // store uses -- which is how the row is matched however isCurrentOwner
  // recognised the owner (guid, Superset id or name).
  const loadedShareSubjects = useMemo(
    () => new Set((detail?.shares ?? []).map(s => s.subject.toLowerCase())),
    [detail],
  );
  const excludeFromSharePicker = useCallback(
    (option: OwnershipSubjectOption) =>
      isCurrentOwner(option) &&
      !loadedShareSubjects.has(option.value.toLowerCase()),
    [isCurrentOwner, loadedShareSubjects],
  );

  const handleTransfer = async (target = transferTarget) => {
    if (!target) return;
    const transferTarget = target;
    setTransferring(true);
    setTransferError(null);
    let assignment: OwnershipOwnerAssignment;
    try {
      assignment = await assignOwner(
        assetType,
        objectId,
        transferTarget.subject,
      );
    } catch (caught) {
      const { message, status, timedOut } = await errorDetails(caught);
      if (timedOut) {
        // The client gave up waiting; the server may still have applied the
        // write. So this must not claim that nothing changed -- only that the
        // outcome is unknown and where to find it out: the detail is read
        // again below, so the summary names whoever owns the object now.
        setTransferError(
          t(
            'The server did not answer in time, so it is not known whether ownership was transferred. Check the current owner below before trying again.',
          ),
        );
      } else {
        // The API's own message, verbatim: it names the actual reason (a
        // deactivated account, a member without access to the dataset, ...)
        // and paraphrasing it here would only lose that.
        setTransferError(
          message ||
            t('Could not transfer ownership. Nothing has been changed.'),
        );
      }
      setTransferring(false);
      // The row's state is no longer trustworthy either way; let the list
      // re-read it.
      if (timedOut) onApplied();
      // Two refusals say the detail on screen is stale. A 403 means the
      // caller stopped managing this object after the drawer was opened. A
      // timeout means the write may have landed: the object may have a new
      // owner, and this caller may no longer manage it. Either way the
      // detail is read again -- the confirmation is inert while that is in
      // flight -- so that the summary names the actual owner and the confirm
      // button follows the actual can_manage, rather than inviting the same
      // refusal, or a second transfer, on the strength of the old detail.
      // When the re-read is refused as well (a non-public object the caller
      // can no longer read at all) the detail is dropped and there is
      // nothing left to confirm against: back to the main view, which then
      // offers Close and nothing that writes.
      if (timedOut || status === 403) {
        const reread = await loadDetail(
          timedOut
            ? t(
                'The server did not answer in time, so it is not known whether ownership was transferred, and the latest details could not be loaded. Close and reopen this panel to see the current owner.',
              )
            : rereadFailedMessage,
        );
        if (!reread) {
          setTransferTarget(null);
          setView('main');
        }
      }
      return;
    }
    // The row's owner, can_manage and can_share have all changed; the list
    // re-reads them the same way it does after a visibility change.
    onApplied();
    let refreshed: OwnershipDetail | null = null;
    let outcome: TransferOutcome;
    try {
      refreshed = await fetchOwnership(assetType, objectId);
      outcome = refreshed.can_manage ? 'managed' : 'lost';
    } catch (caught) {
      // A private object's previous owner can no longer read it at all (the
      // API answers 404; a 403 says the same), which for this drawer is the
      // same outcome as reading it back and finding can_manage false. Any
      // other failure says nothing about the caller's access.
      const { status } = await errorDetails(caught);
      outcome = status === 403 || status === 404 ? 'lost' : 'unread';
    }
    setDetail(refreshed);
    if (refreshed) {
      if (refreshed.visibility) setVisibility(refreshed.visibility);
      setShares(refreshed.shares);
    }
    // Name the owner the server confirmed where the re-read shows the account
    // the PUT reported; otherwise fall back to the name that was picked.
    const confirmedOwner =
      refreshed?.owner && refreshed.owner.id === assignment.owner_user_id
        ? refreshed.owner.name
        : transferTarget.name;
    setTransferred({
      to: confirmedOwner,
      outcome,
      reason: refreshed?.manage_reason,
    });
    setTransferTarget(null);
    setTransferring(false);
    setView('main');
  };

  const handleApply = async () => {
    if (!detail) return;
    setApplying(true);
    // Thunks, not promises: calling the api functions here would start every
    // request at once, and awaiting an already-running request cannot stop
    // the ones behind it.
    const steps: (() => Promise<void>)[] = [];
    if (visibility !== detail.visibility) {
      steps.push(() => updateVisibility(assetType, objectId, visibility));
    }
    if (visibility === 'shared') {
      const originalKeys = subjectKeys(detail.shares);
      const stagedKeys = subjectKeys(shares);
      const originalRole = new Map(detail.shares.map(s => [s.subject, s.role]));
      shares
        // Added subjects, and subjects whose role changed -- POST /shares
        // upserts, so both are the same call. Without the second clause a
        // demotion produced zero requests.
        .filter(
          s =>
            !originalKeys.has(s.subject) ||
            originalRole.get(s.subject) !== s.role,
        )
        .forEach(s =>
          steps.push(() => addShare(assetType, objectId, s.subject, s.role)),
        );
      detail.shares
        .filter(s => !stagedKeys.has(s.subject))
        .forEach(s =>
          steps.push(() => removeShare(assetType, objectId, s.subject)),
        );
    }
    try {
      // Sequential and ordered: the visibility change lands before the share
      // writes that depend on it, and a rejection stops the rest rather than
      // leaving a half-applied mix.
      // eslint-disable-next-line no-restricted-syntax
      for (const step of steps) {
        // eslint-disable-next-line no-await-in-loop
        await step();
      }
    } catch (caught) {
      setApplying(false);
      // The API's own sentence when it gave one: a refusal names its reason
      // (a permission that does not include making the object public, or
      // changing the caller's own share), and the generic line alone left
      // the caller to guess which of the staged changes was the problem.
      // Steps before the failed one have landed, so the second sentence
      // stays: the panel's state is no longer the object's.
      const { message } = await errorDetails(caught);
      setError(
        message
          ? t(
              'Could not save these changes: %s. Reopen this panel to see the current settings.',
              message,
            )
          : t(
              'Could not save these changes. Reopen this panel to see the current settings.',
            ),
      );
      // The row's state is no longer trustworthy either way, so let the list
      // re-read it -- but keep the panel open on the failure.
      onApplied();
      return;
    }
    setApplying(false);
    onApplied();
    onClose();
  };

  // The picker is seeded with the current shares and returns the caller's
  // COMPLETE selection, so it replaces the staged list. Merging it instead
  // meant the list could only ever grow: unchecking someone came back as a
  // selection that simply omitted them, the merge kept them anyway, the diff
  // against `detail.shares` was empty and Apply stayed disabled -- there was
  // no way to revoke a share from this drawer at all.
  //
  // The picker returns subjects, not shares. A subject that was already
  // staged keeps its row (and its role); a new one is granted 'viewer' --
  // least privilege, and the same relation for both kinds. Granting a person
  // 'editor' meant the local authorizer -- the default backend -- matched no
  // read check at all: the share was written, reported, listed, and conferred
  // nothing.
  const handleSubjectsSelected = (selected: SubjectRef[]) => {
    const staged = new Map(shares.map(s => [s.subject, s]));
    setShares(
      selected.map(
        s =>
          staged.get(s.subject) ?? {
            subject: s.subject,
            name: s.name,
            role: 'viewer',
          },
      ),
    );
    setView('main');
  };

  const backTitle = (label: string, onBack: () => void, disabled = false) => (
    <Button
      buttonStyle="link"
      onClick={onBack}
      disabled={disabled}
      data-test="sharing-drawer-back"
      css={css`
        padding: 0;
        display: flex;
        align-items: center;
        gap: ${theme.sizeUnit}px;
      `}
    >
      <Icons.LeftOutlined iconSize="m" />
      {label}
    </Button>
  );

  // What the last transfer came to. A live region that is in the drawer from
  // the start, and in every view, so that a screen reader announces the text
  // when it arrives: a region mounted together with its content is not
  // reliably read out. The same element persists across the views because
  // the drawer itself never unmounts (open never toggles) and this is always
  // its first, keyed child.
  const transferStatus = (
    <div key="transfer-status" role="status" data-test="transfer-status">
      {transferred && (
        <Banner
          tone={transferred.outcome === 'unread' ? 'warning' : 'success'}
          data-test="transfer-success"
        >
          {transferred.outcome === 'unread' ? (
            <Icons.WarningOutlined
              iconSize="m"
              iconColor={theme.colorWarning}
            />
          ) : (
            <Icons.CheckCircleOutlined
              iconSize="m"
              iconColor={theme.colorSuccess}
            />
          )}
          <span>{transferredMessage(transferred)}</span>
        </Banner>
      )}
    </div>
  );

  if (view === 'picker') {
    return (
      <Drawer
        open
        placement="right"
        width={420}
        closable={false}
        onClose={onClose}
        title={backTitle(t('Share with users/groups'), () => setView('main'))}
      >
        {transferStatus}
        <SubjectPickerPanel
          // Never offer the owner: they already have full access, and the
          // drawer's header names them. The one exception is a stale share
          // row naming the owner (see excludeFromSharePicker).
          exclude={excludeFromSharePicker}
          initiallySelected={shares}
          lockedReason={lockedShareReason}
          onCancel={() => setView('main')}
          onOk={handleSubjectsSelected}
        />
      </Drawer>
    );
  }

  if (view === 'transfer') {
    return (
      <Drawer
        open
        placement="right"
        width={420}
        closable={false}
        onClose={onClose}
        title={backTitle(t('Transfer ownership'), () => setView('main'))}
      >
        {transferStatus}
        <SubjectPickerPanel
          // Users of the caller's tenant only (that is all /subjects returns),
          // one of them, and never the person who owns it already.
          kinds={USERS_ONLY}
          selectionMode="single"
          exclude={isCurrentOwner}
          initiallySelected={transferTarget ? [transferTarget] : []}
          okLabel={t('Continue')}
          onCancel={() => setView('main')}
          onOk={selected => {
            setTransferTarget(selected[0] ?? null);
            setTransferError(null);
            setView('transfer-confirm');
          }}
        />
      </Drawer>
    );
  }

  if (view === 'transfer-confirm' && detail && transferTarget) {
    const currentOwner = detail.owner?.name ?? t('the current owner');
    return (
      <Drawer
        open
        placement="right"
        width={420}
        closable={false}
        // While the PUT is in flight there is no way out of this view: a
        // second pick or a closed drawer would race the write that is about
        // to land and either discard the pick or hide the outcome.
        onClose={transferring ? undefined : onClose}
        keyboard={!transferring}
        mask={{ closable: !transferring }}
        title={backTitle(
          t('Transfer ownership'),
          () => setView('transfer'),
          transferring,
        )}
        footer={
          <Footer>
            <Button
              buttonStyle="secondary"
              disabled={transferring}
              onClick={() => setView('main')}
            >
              {t('Cancel')}
            </Button>
            <Button
              buttonStyle="primary"
              loading={transferring}
              // A re-read after a refusal may have found that the caller no
              // longer manages the object; retrying would only be refused
              // again. And while that re-read is in flight the answer is not
              // in yet, so the button waits for it.
              disabled={!detail.can_manage || loading}
              onClick={() => handleTransfer()}
              data-test="confirm-transfer-ownership"
            >
              {t('Transfer ownership')}
            </Button>
          </Footer>
        }
      >
        {transferStatus}
        {transferError && (
          <Banner tone="error" role="alert" data-test="ownership-error">
            <Icons.ExclamationCircleOutlined
              iconSize="m"
              iconColor={theme.colorError}
            />
            <span>{transferError}</span>
          </Banner>
        )}
        <ObjectHeading ref={confirmHeadingRef} tabIndex={-1}>
          {objectTitle}
        </ObjectHeading>
        <div data-test="transfer-summary">
          {t(
            'Transfer ownership of this object from %s to %s?',
            currentOwner,
            transferTarget.name,
          )}
        </div>
        <Consequences>
          <li>
            {t(
              '%s will become the owner and will decide who it is shared with from now on.',
              transferTarget.name,
            )}
          </li>
          <li>
            {t(
              '%s will no longer own it and will lose the ability to manage its sharing.',
              currentOwner,
            )}
          </li>
          <li>
            {t(
              'Existing shares are unaffected: everyone it is already shared with keeps their access.',
            )}
          </li>
          {detail.visibility !== 'public' && (
            // The server's visibility, not the staged radio: it is what the
            // backend will enforce. Only a public object can be opened by
            // someone with no relation to it; nothing is written for the
            // previous owner as a viewer, and an owner does not normally hold
            // a share on their own object, so a shared object closes to them
            // exactly as a private one does.
            <li data-test="transfer-access-consequence">
              {t(
                'This object is not public. Unless it is shared with %s, they will no longer be able to open it.',
                currentOwner,
              )}
            </li>
          )}
        </Consequences>
      </Drawer>
    );
  }

  return (
    <Drawer
      open
      placement="right"
      width={420}
      onClose={onClose}
      title={t('Sharing')}
      footer={
        <Footer>
          <Button
            buttonStyle="secondary"
            onClick={onClose}
            data-test="sharing-drawer-dismiss"
          >
            {readOnly || transferOnly ? t('Close') : t('Cancel')}
          </Button>
          {!readOnly && !transferOnly && (
            <Button
              buttonStyle="primary"
              disabled={!dirty || applying || needsOwner}
              loading={applying}
              onClick={handleApply}
            >
              {t('Apply')}
            </Button>
          )}
        </Footer>
      }
    >
      {transferStatus}
      {error && (
        <Banner tone="error" role="alert" data-test="ownership-error">
          <Icons.ExclamationCircleOutlined
            iconSize="m"
            iconColor={theme.colorError}
          />
          <span>{error}</span>
        </Banner>
      )}
      {loading && <Loading />}
      {detail && (
        <>
          {needsOwner && (
            <Banner tone="warning" data-test="unowned-notice">
              <Icons.WarningOutlined
                iconSize="m"
                iconColor={theme.colorWarning}
              />
              <span
                css={css`
                  display: flex;
                  flex-direction: column;
                  gap: ${theme.sizeUnit * 2}px;
                `}
              >
                <span>
                  {detail.unowned
                    ? t(
                        'This object’s owner account has been deactivated. Assign a new owner before it can be shared.',
                      )
                    : t(
                        'This object has no owner yet. Assign an owner before it can be shared.',
                      )}
                </span>
                {canAssignOwner && (
                  <span>
                    <Button
                      buttonStyle="primary"
                      buttonSize="small"
                      loading={claiming}
                      onClick={handleClaim}
                      data-test="assign-owner-to-me"
                    >
                      {t('Assign owner to me')}
                    </Button>
                  </span>
                )}
              </span>
            </Banner>
          )}

          <Banner tone="info">
            <Icons.InfoCircleOutlined iconSize="m" />
            <span>
              {t(
                'Updating the share access level may grant or remove sharing access for existing users and groups. Review changes before applying.',
              )}
            </span>
          </Banner>
        </>
      )}

      {/* Outside the detail block on purpose: the object stays named when
          the read failed or the caller lost access to it after a transfer,
          and it is where focus lands on the way back from a sub-view. */}
      <ObjectHeading ref={mainHeadingRef} tabIndex={-1}>
        {objectTitle}
      </ObjectHeading>

      {detail && (
        <>
          {managingAsNotice && (
            <Banner tone="warning" data-test="managing-as-notice">
              <Icons.WarningOutlined
                iconSize="m"
                iconColor={theme.colorWarning}
              />
              <span>{managingAsNotice}</span>
            </Banner>
          )}
          {transferError && (
            // "Take ownership" writes from this view, so its refusal is
            // shown here; the transfer sub-view shows its own.
            <Banner tone="error" role="alert" data-test="take-ownership-error">
              <Icons.ExclamationCircleOutlined
                iconSize="m"
                iconColor={theme.colorError}
              />
              <span>{transferError}</span>
            </Banner>
          )}

          {detail.owner && !needsOwner && (
            <OwnerRow data-test="owner-row">
              <span>{t('Owner: %s', detail.owner.name)}</span>
              {canTransfer && transferOnly && detail.caller_subject && (
                // A transfer to the caller: the step a tenant administrator
                // takes before changing the sharing. Same write as a
                // transfer to anyone else, and the owner line then names
                // them.
                <Button
                  buttonStyle="link"
                  buttonSize="small"
                  loading={transferring}
                  onClick={async () => {
                    setTransferred(null);
                    setTransferError(null);
                    await handleTransfer({
                      subject: detail.caller_subject as string,
                      name: t('you'),
                    });
                    // The button is gone once the caller owns the object
                    // (and inert while the outcome is unknown); the view
                    // did not change, so focus is placed by hand.
                    mainHeadingRef.current?.focus();
                  }}
                  data-test="take-ownership"
                  css={css`
                    padding: 0;
                  `}
                >
                  <Icons.UserOutlined iconSize="s" /> {t('Take ownership')}
                </Button>
              )}
              {canTransfer && (
                // Staged visibility/share changes are not carried across a
                // transfer: the re-read afterwards replaces them, and after a
                // transfer away the caller could not apply them anyway. So
                // the two are sequenced explicitly rather than one silently
                // discarding the other.
                <Button
                  buttonStyle="link"
                  buttonSize="small"
                  disabled={dirty || transferring}
                  tooltip={
                    dirty
                      ? t('Apply or discard your pending changes first')
                      : undefined
                  }
                  onClick={() => {
                    // A new transfer starts with a clean slate: the last
                    // one's banner would otherwise follow the caller into
                    // the picker and be announced a second time on return.
                    setTransferred(null);
                    setTransferError(null);
                    setView('transfer');
                  }}
                  data-test="transfer-ownership"
                  css={css`
                    padding: 0;
                  `}
                >
                  <Icons.UserOutlined iconSize="s" /> {t('Transfer ownership')}
                </Button>
              )}
            </OwnerRow>
          )}

          {/* Radio.Group with its own Radio children rather than the
              GroupWrapper: the wrapper renders every option enabled, and
              one of these -- Public -- has to be disabled on its own for a
              holder of the manage-sharing permission, whose Apply the
              backend would otherwise refuse. */}
          <Radio.Group
            value={visibility}
            onChange={e => setVisibility(e.target.value)}
            disabled={radiosDisabled}
          >
            <Space direction="vertical" size={theme.sizeUnit * 4}>
              <Radio value="private">
                <AccessOption>
                  <div className="option-title">{t('Private')}</div>
                  <div className="option-description">
                    {t('Visible to only you. Existing shares are removed.')}
                  </div>
                </AccessOption>
              </Radio>
              <Radio value="shared">
                <AccessOption>
                  <div className="option-title">{t('Shared')}</div>
                  <div className="option-description">
                    {t('Shared with selected users or groups')}
                  </div>
                </AccessOption>
              </Radio>
              <Radio
                value="public"
                // An explicit value here takes precedence over the group's:
                // antd reads the radio's own `disabled` first and falls
                // back to the group only when it is undefined.
                disabled={radiosDisabled || managesSharingOnly}
                data-test="visibility-public"
              >
                <AccessOption>
                  <div className="option-title">{t('Public')}</div>
                  <div className="option-description">
                    {managesSharingOnly
                      ? t(
                          'Shared with users in your organisation. Only the owner or an administrator can make this object public.',
                        )
                      : t('Shared with users in your organisation')}
                  </div>
                </AccessOption>
              </Radio>
            </Space>
          </Radio.Group>

          {visibility === 'shared' && (
            <SharedSummary>
              <div>{t('Shared with %s users', userCount)}</div>
              <div>{t('Shared with %s groups', groupCount)}</div>

              <Button
                buttonStyle="link"
                disabled={needsOwner || readOnly || transferOnly}
                onClick={() => {
                  setTransferred(null);
                  setView('picker');
                }}
                css={css`
                  padding: 0;
                  align-self: flex-start;
                `}
              >
                <Icons.PlusOutlined iconSize="s" /> {t('Add users & groups')}
              </Button>
            </SharedSummary>
          )}
        </>
      )}
    </Drawer>
  );
}
