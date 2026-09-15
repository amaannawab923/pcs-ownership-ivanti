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

import { useEffect, useMemo, useState } from 'react';
import { t } from '@apache-superset/core/translation';
import { css, useTheme } from '@apache-superset/core/theme';
import {
  Button,
  Checkbox,
  Input,
  Loading,
  Radio,
  Tabs,
} from '@superset-ui/core/components';
import { searchSubjects } from './api';
import type { OwnershipSubjectOption, SubjectKind, SubjectRef } from './types';

// Stable defaults: fresh values on every render would re-run the filters
// below each time the parent re-renders.
const ALL_KINDS: SubjectKind[] = ['user', 'group'];
const OFFER_ALL = () => false;

// What a row shows, and what its control is called. A person is listed by
// display name with the email underneath; /subjects labels a member Superset
// has no name for by their raw GUID, in which case the email (when there is
// one) is the better name and is not repeated as the detail line. A group is
// its name over a member count.
export function rowLabels(option: OwnershipSubjectOption): {
  primary: string;
  secondary: string | null;
} {
  if (option.extra.type === 'group') {
    return {
      primary: option.text,
      // `members` is null on the directory's fast path (the additive
      // group.tenant relation, read without a per-group count); the count
      // itself is worth a store round trip only when the walk already paid
      // for it.
      secondary:
        option.extra.members == null
          ? t('from directory')
          : t('%s member(s) · from directory', option.extra.members),
    };
  }
  const { guid, email } = option.extra;
  const named = option.text && option.text !== guid;
  const primary = named ? option.text : email || option.text;
  return {
    primary,
    secondary: email && email !== primary ? email : null,
  };
}

export interface SubjectPickerPanelProps {
  initiallySelected: SubjectRef[];
  onCancel: () => void;
  // The complete selection, subjects and display names only. What a picked
  // subject is FOR -- a share with a role, the new owner -- is the caller's
  // business, not the picker's.
  onOk: (selected: SubjectRef[]) => void;
  // Which kinds of subject to offer. Defaults to both; the transfer-ownership
  // flow passes ['user'] because an owner can only ever be a person -- the
  // backend rejects a group -- and a tab of unselectable groups would only
  // invite the rejection.
  kinds?: SubjectKind[];
  // 'multiple' (default) is the share picker: checkboxes, an empty selection
  // is legitimate. 'single' picks exactly one subject and the Ok button waits
  // for it.
  selectionMode?: 'multiple' | 'single';
  // Rows to leave out of the list altogether -- the current owner, in the
  // transfer flow, since transferring to them is a no-op. A predicate rather
  // than a list of subjects because the owner is not always identifiable by
  // subject alone (see SharingDrawer).
  exclude?: (option: OwnershipSubjectOption) => boolean;
  // Rows to show but not let the caller change, with the sentence that says
  // why -- the caller's own share, or the tenant's own membership or
  // administrator group, under a permission that leaves those rows to the
  // owner. The row stays listed (and stays in the selection if it was) so
  // the caller can see it is there; only the checkbox is inert. Null for a
  // row the caller may change. Share picker only.
  lockedReason?: (option: OwnershipSubjectOption) => string | null;
  okLabel?: string;
}

const NONE_LOCKED = () => null;

export default function SubjectPickerPanel({
  initiallySelected,
  onCancel,
  onOk,
  kinds = ALL_KINDS,
  selectionMode = 'multiple',
  exclude = OFFER_ALL,
  lockedReason = NONE_LOCKED,
  okLabel,
}: SubjectPickerPanelProps) {
  const theme = useTheme();
  const single = selectionMode === 'single';
  const usersOnly = !kinds.includes('group');
  const [activeTab, setActiveTab] = useState<SubjectKind>(kinds[0] ?? 'user');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(false);
  // The last /subjects request failed. Shown in place of the list rather
  // than left as a spinner that never stops, and cleared by the next
  // request (a new query).
  const [loadFailed, setLoadFailed] = useState(false);
  const [results, setResults] = useState<OwnershipSubjectOption[]>([]);
  // The caller's tenant as /subjects reports it. Null once a response has
  // said so; undefined until one arrives.
  const [tenant, setTenant] = useState<string | null | undefined>(undefined);
  // F-2: /subjects answered 200 but could not fully resolve the directory
  // or authorization store, so `results` may be incomplete -- an empty
  // list here is not necessarily "nobody matched" and must not read as one.
  const [degraded, setDegraded] = useState(false);
  const [selected, setSelected] = useState<Map<string, SubjectRef>>(
    () => new Map(initiallySelected.map(s => [s.subject, s])),
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    const timeout = setTimeout(() => {
      searchSubjects(query)
        .then(response => {
          if (cancelled) return;
          setResults(response.result);
          setTenant(response.tenant);
          setDegraded(!!response.degraded);
          setLoadFailed(false);
          setLoading(false);
        })
        .catch(() => {
          if (cancelled) return;
          setResults([]);
          setDegraded(false);
          setLoadFailed(true);
          setLoading(false);
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timeout);
    };
  }, [query]);

  const offered = useMemo(
    () => results.filter(r => kinds.includes(r.extra.type) && !exclude(r)),
    [results, kinds, exclude],
  );

  const filteredResults = useMemo(
    () => offered.filter(r => r.extra.type === activeTab),
    [offered, activeTab],
  );

  // Counts shown on the tabs are how many subjects are AVAILABLE in each tab
  // (respecting the current search), not how many are selected -- showing the
  // selected count there read as "only 1 user exists". Selection feedback
  // moves to the Ok button.
  const availableUsers = useMemo(
    () => offered.filter(r => r.extra.type === 'user').length,
    [offered],
  );
  const availableGroups = useMemo(
    () => offered.filter(r => r.extra.type === 'group').length,
    [offered],
  );
  const selectedCount = selected.size;

  const toggle = (option: OwnershipSubjectOption, checked: boolean) => {
    setSelected(prev => {
      // Single selection replaces rather than accumulates.
      const next = single ? new Map() : new Map(prev);
      if (checked) {
        // The same name the row shows, so the confirmation and the share
        // list call the person what the picker did.
        next.set(option.value, {
          subject: option.value,
          name: rowLabels(option).primary,
        });
      } else {
        next.delete(option.value);
      }
      return next;
    });
  };
  // In single mode the radios form one group: the group's value is the one
  // selected subject, and a change is a pick of the row with that value.
  const singleValue = single ? ([...selected.keys()][0] ?? null) : null;
  const pickByValue = (value: string) => {
    const option = filteredResults.find(r => r.value === value);
    if (option) toggle(option, true);
  };

  // An empty list means different things. With a query: nothing matched.
  // Without one, in the transfer picker: there is nobody to hand the object
  // to -- and when the caller has no tenant that is not a transient state,
  // since /subjects lists a tenant's members and nothing else.
  const emptyMessage = () => {
    // F-2: the endpoint already told us it could not fully answer -- an
    // empty list here is not "nobody matched", and saying so would read to
    // an Ivanti operator as an empty directory rather than the outage it
    // is. Takes priority over every other empty-list explanation below.
    if (degraded) {
      return t(
        'The directory is temporarily unavailable. Try again in a moment.',
      );
    }
    if (query || !usersOnly) return t('No results found');
    if (tenant === null) {
      return t(
        'No users available to transfer to. Your account is not in a tenant, so there is no member list to choose from; an administrator can name the new owner through the API.',
      );
    }
    return t(
      'No users available to transfer to. Only members of your tenant other than the current owner can be chosen.',
    );
  };

  const rows = filteredResults.map(option => {
    const { primary, secondary } = rowLabels(option);
    const locked = single ? null : lockedReason(option);
    return (
      <div
        key={option.value}
        css={css`
          display: flex;
          align-items: center;
          gap: ${theme.sizeUnit * 2}px;
          padding: ${theme.sizeUnit * 2}px 0;
          border-bottom: 1px solid ${theme.colorBorderSecondary};
        `}
      >
        {single ? (
          <Radio value={option.value} aria-label={primary} />
        ) : (
          <Checkbox
            checked={selected.has(option.value)}
            onChange={e => toggle(option, e.target.checked)}
            aria-label={primary}
            disabled={locked !== null}
          />
        )}
        <div>
          <div data-test="subject-name">{primary}</div>
          {locked && (
            <div
              data-test="subject-locked"
              css={css`
                color: ${theme.colorTextSecondary};
                font-size: ${theme.fontSizeSM}px;
              `}
            >
              {locked}
            </div>
          )}
          {secondary && (
            <div
              data-test="subject-detail"
              css={css`
                color: ${theme.colorTextSecondary};
                font-size: ${theme.fontSizeSM}px;
              `}
            >
              {secondary}
            </div>
          )}
        </div>
      </div>
    );
  });

  return (
    <div
      css={css`
        display: flex;
        flex-direction: column;
        height: 100%;
      `}
    >
      <Input
        placeholder={
          usersOnly ? t('Search users') : t('Search users and groups')
        }
        value={query}
        onChange={e => setQuery(e.target.value)}
        allowClear
        // The panel replaces the drawer's previous content, so whatever had
        // focus is gone; land on the first control of the new view.
        autoFocus
        css={css`
          margin-bottom: ${theme.sizeUnit * 3}px;
        `}
      />
      {!usersOnly && (
        <Tabs
          activeKey={activeTab}
          onChange={key => setActiveTab(key as SubjectKind)}
          items={[
            {
              key: 'user',
              label: t('Users (%s)', availableUsers),
            },
            {
              key: 'group',
              label: t('Groups (%s)', availableGroups),
            },
          ]}
        />
      )}
      <div
        css={css`
          flex: 1;
          overflow-y: auto;
        `}
      >
        {loading ? (
          <Loading />
        ) : loadFailed ? (
          <div
            role="alert"
            data-test="subject-picker-error"
            css={css`
              color: ${theme.colorError};
              padding: ${theme.sizeUnit * 4}px 0;
              text-align: center;
            `}
          >
            {usersOnly
              ? t(
                  'Could not load the list of users. Check your connection and try again.',
                )
              : t(
                  'Could not load the list of users and groups. Check your connection and try again.',
                )}
          </div>
        ) : filteredResults.length === 0 ? (
          <div
            data-test="subject-picker-empty"
            css={css`
              color: ${theme.colorTextSecondary};
              padding: ${theme.sizeUnit * 4}px 0;
              text-align: center;
            `}
          >
            {emptyMessage()}
          </div>
        ) : single ? (
          // One group, one shared name: arrow keys move between the options
          // and Tab treats the list as a single stop, as radios should.
          <Radio.Group
            aria-label={t('Users')}
            value={singleValue}
            onChange={e => pickByValue(e.target.value)}
            css={css`
              display: block;
            `}
          >
            {rows}
          </Radio.Group>
        ) : (
          rows
        )}
      </div>
      <div
        css={css`
          display: flex;
          justify-content: flex-end;
          gap: ${theme.sizeUnit * 2}px;
          padding-top: ${theme.sizeUnit * 3}px;
        `}
      >
        <Button buttonStyle="secondary" onClick={onCancel}>
          {t('Cancel')}
        </Button>
        <Button
          buttonStyle="primary"
          // In the share picker an empty selection is a legitimate outcome --
          // it means "share with nobody". Disabling the button there made
          // clearing the last share impossible from this panel. Single
          // selection has no such outcome: there is nothing to continue with
          // until someone is picked.
          disabled={single && selectedCount === 0}
          onClick={() => onOk([...selected.values()])}
          data-test="subject-picker-ok"
        >
          {okLabel ??
            (selectedCount > 0
              ? t('Confirm (%s)', selectedCount)
              : t('Confirm'))}
        </Button>
      </div>
    </div>
  );
}
