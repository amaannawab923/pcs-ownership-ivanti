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

import fetchMock from 'fetch-mock';
import { SupersetClient } from '@superset-ui/core';
import {
  fireEvent,
  render,
  screen,
  userEvent,
  waitFor,
  within,
} from 'spec/helpers/testing-library';
import SharingDrawer from './SharingDrawer';
import { ASSIGN_OWNER_TIMEOUT_MS } from './api';
import type { OwnershipDetail, OwnershipSubjectOption } from './types';

const DETAIL_ENDPOINT = 'glob:*/api/v1/ownership/chart/7';
const OWNER_ENDPOINT = 'glob:*/api/v1/ownership/chart/7/owner';
const SHARES_ENDPOINT = 'glob:*/api/v1/ownership/chart/7/shares';
const SHARE_DELETE_ENDPOINT = 'glob:*/api/v1/ownership/chart/7/shares/*';
const VISIBILITY_ENDPOINT = 'glob:*/api/v1/ownership/chart/7/visibility';
const SUBJECTS_ENDPOINT = 'glob:*/api/v1/ownership/subjects?*';

const ownedDetail: OwnershipDetail = {
  object_id: 7,
  owner: { id: 1, name: 'Jane Doe', guid: 'jane-guid' },
  visibility: 'private',
  unowned: false,
  can_manage: true,
  can_share: true,
  shares: [],
};

const subjects: OwnershipSubjectOption[] = [
  {
    value: 'user:jane-guid',
    text: 'Jane Doe',
    extra: { type: 'user', guid: 'jane-guid', email: 'jane@example.com' },
  },
  {
    value: 'user:bob-guid',
    text: 'Bob Smith',
    extra: { type: 'user', guid: 'bob-guid', email: 'bob@example.com' },
  },
  {
    value: 'user:carol-guid',
    text: 'Carol Jones',
    extra: { type: 'user', guid: 'carol-guid', email: 'carol@example.com' },
  },
  {
    value: 'group:designers#member',
    text: 'Designers',
    extra: { type: 'group', members: 3, in_superset: false },
  },
];

const PUT_OK = { object_id: 7, owner_user_id: 2 };

// The detail the GET answers with. Tests swap it after the PUT to model what
// the previous owner sees on the re-read; a number models that status with
// an empty message body.
let currentDetail: OwnershipDetail | number = ownedDetail;
// What /subjects answers with: the rows and the caller's tenant, or a status
// when the store is unreachable.
let currentSubjects:
  | { result: OwnershipSubjectOption[]; tenant: string | null }
  | number = {
  result: subjects,
  tenant: 'acme',
};

const onClose = jest.fn();
const onApplied = jest.fn();

beforeEach(() => {
  currentDetail = ownedDetail;
  currentSubjects = { result: subjects, tenant: 'acme' };
  fetchMock.get(DETAIL_ENDPOINT, () =>
    typeof currentDetail === 'number'
      ? { status: currentDetail, body: { message: 'refused' } }
      : { status: 200, body: currentDetail },
  );
  fetchMock.get(SUBJECTS_ENDPOINT, () =>
    typeof currentSubjects === 'number'
      ? { status: currentSubjects, body: { message: 'store unavailable' } }
      : currentSubjects,
  );
});

afterEach(() => {
  fetchMock.clearHistory().removeRoutes();
  jest.clearAllMocks();
  jest.restoreAllMocks();
});

const renderDrawer = () =>
  render(
    <SharingDrawer
      assetType="chart"
      objectId={7}
      objectTitle="Quarterly revenue"
      onClose={onClose}
      onApplied={onApplied}
    />,
  );

const openPicker = async () => {
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByTestId('transfer-ownership'));
  return screen.findByRole('radio', { name: 'Bob Smith' });
};

const pickBobAndContinue = async () => {
  const bob = await openPicker();
  await userEvent.click(bob);
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  return screen.findByTestId('confirm-transfer-ownership');
};

// The drawer's Esc handling is a document-level keydown listener, gated by
// the drawer's `keyboard` prop and by whether it has an `onClose` at all --
// so one Esc exercises both halves of the in-flight lock at once.
const pressEscape = () =>
  fireEvent.keyDown(document.activeElement ?? document.body, {
    key: 'Escape',
  });

// The object stays named whatever the read answered, nothing that writes is
// offered, and the only way out is Close.
const expectReadOnly = () => {
  expect(
    screen.getByRole('heading', { name: 'Quarterly revenue' }),
  ).toBeInTheDocument();
  expect(screen.queryByTestId('transfer-ownership')).not.toBeInTheDocument();
  expect(
    screen.queryByRole('button', { name: 'Apply' }),
  ).not.toBeInTheDocument();
  expect(screen.getByTestId('sharing-drawer-dismiss')).toHaveTextContent(
    'Close',
  );
};

// What the client's request timeout rejects with (rejectAfterTimeout).
const timeoutRejection = (timeout: number) => ({
  error: 'Request timed out',
  statusText: 'timeout',
  timeout,
});

test('offers Transfer ownership on an owned, managed object', async () => {
  renderDrawer();
  const ownerRow = await screen.findByTestId('owner-row');
  expect(ownerRow).toHaveTextContent('Owner: Jane Doe');
  expect(screen.getByTestId('transfer-ownership')).toBeInTheDocument();
  expect(screen.queryByTestId('assign-owner-to-me')).not.toBeInTheDocument();
});

test('hides Transfer ownership when the caller cannot manage', async () => {
  currentDetail = { ...ownedDetail, can_manage: false, can_share: false };
  renderDrawer();
  const ownerRow = await screen.findByTestId('owner-row');
  expect(ownerRow).toHaveTextContent('Owner: Jane Doe');
  expectReadOnly();
  screen.getAllByRole('radio').forEach(radio => expect(radio).toBeDisabled());
});

test('keeps Assign owner to me for an object with no owner', async () => {
  currentDetail = { ...ownedDetail, owner: null, can_share: false };
  renderDrawer();
  expect(await screen.findByTestId('assign-owner-to-me')).toBeInTheDocument();
  expect(screen.queryByTestId('transfer-ownership')).not.toBeInTheDocument();
  expect(screen.queryByTestId('owner-row')).not.toBeInTheDocument();
});

test('keeps Assign owner to me when the owner is deactivated', async () => {
  currentDetail = { ...ownedDetail, unowned: true, can_share: false };
  renderDrawer();
  expect(await screen.findByTestId('assign-owner-to-me')).toBeInTheDocument();
  expect(screen.queryByTestId('transfer-ownership')).not.toBeInTheDocument();
});

test('offers no control on an unowned, unmanaged object', async () => {
  currentDetail = { ...ownedDetail, owner: null, can_manage: false };
  renderDrawer();
  await screen.findByTestId('unowned-notice');
  expect(screen.queryByTestId('assign-owner-to-me')).not.toBeInTheDocument();
  expect(screen.queryByTestId('transfer-ownership')).not.toBeInTheDocument();
});

test('the transfer picker is users only, single, minus the owner', async () => {
  renderDrawer();
  await openPicker();

  expect(screen.queryByText('Designers')).not.toBeInTheDocument();
  expect(screen.queryByText(/Groups \(/)).not.toBeInTheDocument();
  expect(screen.queryByText('Jane Doe')).not.toBeInTheDocument();

  // One radio group with an accessible name, and one shared name across the
  // radios so the arrow keys move between them.
  const group = screen.getByRole('radiogroup', { name: 'Users' });
  const radios = within(group).getAllByRole('radio');
  expect(radios).toHaveLength(2);
  expect(radios[0]).toHaveAttribute('name');
  expect(radios[0].getAttribute('name')).not.toBe('');
  expect(radios[1].getAttribute('name')).toBe(radios[0].getAttribute('name'));

  const next = screen.getByTestId('subject-picker-ok');
  expect(next).toHaveTextContent('Continue');
  expect(next).toBeDisabled();

  await userEvent.click(screen.getByRole('radio', { name: 'Bob Smith' }));
  expect(next).toBeEnabled();
  // A second pick replaces the first.
  await userEvent.click(screen.getByRole('radio', { name: 'Carol Jones' }));
  expect(screen.getByRole('radio', { name: 'Bob Smith' })).not.toBeChecked();
  expect(screen.getByRole('radio', { name: 'Carol Jones' })).toBeChecked();
});

test('excludes the owner when only the case of the guid differs', async () => {
  // The detail reports the username as spelled; the store lower-cases it.
  // The name is spelled differently on purpose, so that only the GUID can
  // account for the exclusion.
  currentDetail = {
    ...ownedDetail,
    owner: { id: 1, name: 'Doe, Jane', guid: 'JANE-GUID' },
  };
  renderDrawer();
  await openPicker();
  expect(screen.queryByText('Jane Doe')).not.toBeInTheDocument();
  expect(screen.getAllByRole('radio')).toHaveLength(2);
});

test('excludes the owner by Superset id when the guids disagree', async () => {
  // A local instance: the owner's username is not a GUID, so the store --
  // and /subjects -- know the same account as local-<id>, and Superset has
  // no label for that spelling.
  currentDetail = {
    ...ownedDetail,
    owner: { id: 1, name: 'Jane Doe', guid: 'admin' },
  };
  currentSubjects = {
    tenant: 'acme',
    result: [
      {
        value: 'user:local-1',
        text: 'local-1',
        extra: { type: 'user', guid: 'local-1', id: 1, email: null },
      },
      ...subjects.slice(1),
    ],
  };
  renderDrawer();
  await openPicker();
  expect(screen.queryByText('local-1')).not.toBeInTheDocument();
  expect(screen.getAllByRole('radio')).toHaveLength(2);
});

test('excludes the owner by name when /subjects reports no id', async () => {
  // An older /subjects: no `extra.id` on any row (the fixture rows carry
  // none), and the guids disagree, so the display name is all there is.
  currentDetail = {
    ...ownedDetail,
    owner: { id: 99, name: 'Jane Doe', guid: 'admin' },
  };
  renderDrawer();
  await openPicker();
  expect(screen.queryByText('Jane Doe')).not.toBeInTheDocument();
  expect(screen.getAllByRole('radio')).toHaveLength(2);
});

test('offers a namesake of the owner when the ids differ', async () => {
  // Two people in the tenant called Jane Doe. The owner's row is known by
  // its Superset id; the other Jane is a different account and must be
  // offered, not silently dropped for sharing a name.
  currentDetail = {
    ...ownedDetail,
    owner: { id: 1, name: 'Jane Doe', guid: 'admin' },
  };
  currentSubjects = {
    tenant: 'acme',
    result: [
      {
        value: 'user:local-1',
        text: 'Jane Doe',
        extra: { type: 'user', guid: 'local-1', id: 1, email: null },
      },
      {
        value: 'user:jane2-guid',
        text: 'Jane Doe',
        extra: {
          type: 'user',
          guid: 'jane2-guid',
          id: 5,
          email: 'jane.doe2@example.com',
        },
      },
      {
        value: 'user:bob-guid',
        text: 'Bob Smith',
        extra: {
          type: 'user',
          guid: 'bob-guid',
          id: 2,
          email: 'bob@example.com',
        },
      },
    ],
  };
  renderDrawer();
  await openPicker();
  const janes = screen.getAllByRole('radio', { name: 'Jane Doe' });
  expect(janes).toHaveLength(1);
  expect(
    within(janes[0].closest('div') as HTMLElement).getByTestId(
      'subject-detail',
    ),
  ).toHaveTextContent('jane.doe2@example.com');
  expect(screen.getAllByRole('radio')).toHaveLength(2);
});

test('lists people by name with the email underneath', async () => {
  currentSubjects = {
    tenant: 'acme',
    result: [
      ...subjects.slice(0, 2),
      // A member Superset has no name for: /subjects labels the row by GUID.
      {
        value: 'user:dave-guid',
        text: 'dave-guid',
        extra: { type: 'user', guid: 'dave-guid', email: 'dave@example.com' },
      },
    ],
  };
  renderDrawer();
  await openPicker();

  const bob = screen
    .getByRole('radio', { name: 'Bob Smith' })
    .closest('div') as HTMLElement;
  expect(within(bob).getByTestId('subject-name')).toHaveTextContent(
    'Bob Smith',
  );
  expect(within(bob).getByTestId('subject-detail')).toHaveTextContent(
    'bob@example.com',
  );

  // The email stands in for the missing name, once, not as name and detail.
  const dave = screen.getByRole('radio', { name: 'dave@example.com' });
  expect(dave).toBeInTheDocument();
  expect(screen.queryByText('dave-guid')).not.toBeInTheDocument();
  expect(screen.getAllByText('dave@example.com')).toHaveLength(1);

  // The confirmation calls the person what the picker did.
  await userEvent.click(dave);
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  expect(await screen.findByTestId('transfer-summary')).toHaveTextContent(
    'to dave@example.com?',
  );
});

test('moves focus into the picker and then the confirmation', async () => {
  renderDrawer();
  await openPicker();
  expect(screen.getByPlaceholderText('Search users')).toHaveFocus();

  await userEvent.click(screen.getByRole('radio', { name: 'Bob Smith' }));
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  await screen.findByTestId('confirm-transfer-ownership');
  expect(
    screen.getByRole('heading', { name: 'Quarterly revenue' }),
  ).toHaveFocus();
});

test('moves focus back onto the object heading on the way back', async () => {
  renderDrawer();
  await openPicker();
  await userEvent.click(screen.getByTestId('sharing-drawer-back'));
  await screen.findByTestId('owner-row');
  expect(
    screen.getByRole('heading', { name: 'Quarterly revenue' }),
  ).toHaveFocus();

  await pickBobAndContinue();
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await screen.findByTestId('owner-row');
  expect(
    screen.getByRole('heading', { name: 'Quarterly revenue' }),
  ).toHaveFocus();
});

test('shows an error in the picker when /subjects fails', async () => {
  currentSubjects = 500;
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByTestId('transfer-ownership'));

  const failure = await screen.findByTestId('subject-picker-error');
  expect(failure).toHaveAttribute('role', 'alert');
  expect(failure).toHaveTextContent('Could not load the list of users.');
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  expect(screen.queryByTestId('subject-picker-empty')).not.toBeInTheDocument();
  expect(screen.getByTestId('subject-picker-ok')).toBeDisabled();
});

test('explains an empty picker for a caller with no tenant', async () => {
  currentSubjects = { result: [], tenant: null };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByTestId('transfer-ownership'));

  const empty = await screen.findByTestId('subject-picker-empty');
  expect(empty).toHaveTextContent('No users available to transfer to.');
  expect(empty).toHaveTextContent('not in a tenant');
  expect(screen.getByTestId('subject-picker-ok')).toBeDisabled();
});

test('explains an empty picker when the tenant has nobody else', async () => {
  // The owner is the tenant's only member, and the owner is never offered.
  currentSubjects = { result: [subjects[0]], tenant: 'acme' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByTestId('transfer-ownership'));

  const empty = await screen.findByTestId('subject-picker-empty');
  expect(empty).toHaveTextContent('No users available to transfer to.');
  expect(empty).toHaveTextContent('other than the current owner');
  expect(empty).not.toHaveTextContent('not in a tenant');
});

test('asks for confirmation before anything is written', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK);
  renderDrawer();
  await pickBobAndContinue();

  expect(screen.getByTestId('transfer-summary')).toHaveTextContent(
    'Transfer ownership of this object from Jane Doe to Bob Smith?',
  );
  expect(
    screen.getByText(
      /Jane Doe will no longer own it and will lose the ability/,
    ),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/Existing shares are unaffected/),
  ).toBeInTheDocument();
  expect(fetchMock.callHistory.calls(OWNER_ENDPOINT)).toHaveLength(0);
  expect(onApplied).not.toHaveBeenCalled();
});

test.each(['private', 'shared'] as const)(
  'warns that a %s object closes to its previous owner',
  async visibility => {
    // Either way the backend keeps a non-owner out unless they hold a share,
    // and an owner does not normally hold one on their own object.
    currentDetail = { ...ownedDetail, visibility };
    renderDrawer();
    await pickBobAndContinue();
    expect(screen.getByTestId('transfer-access-consequence')).toHaveTextContent(
      'This object is not public. Unless it is shared with Jane Doe, they ' +
        'will no longer be able to open it.',
    );
  },
);

test('omits the loss-of-access warning for a public object', async () => {
  currentDetail = { ...ownedDetail, visibility: 'public' };
  renderDrawer();
  await pickBobAndContinue();
  expect(
    screen.queryByTestId('transfer-access-consequence'),
  ).not.toBeInTheDocument();
  expect(
    screen.queryByText(/no longer be able to open/),
  ).not.toBeInTheDocument();
});

test('Cancel writes nothing; Back keeps the pick in the picker', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK);
  renderDrawer();
  await pickBobAndContinue();

  await userEvent.click(screen.getByTestId('sharing-drawer-back'));
  expect(await screen.findByRole('radio', { name: 'Bob Smith' })).toBeChecked();

  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  await screen.findByTestId('confirm-transfer-ownership');
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));

  expect(await screen.findByTestId('transfer-ownership')).toBeInTheDocument();
  expect(fetchMock.callHistory.calls(OWNER_ENDPOINT)).toHaveLength(0);
  expect(onApplied).not.toHaveBeenCalled();
});

test('sends the PUT on confirm and refreshes the drawer and list', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK);
  renderDrawer();
  await screen.findByTestId('owner-row');
  // The live region is there, empty, before anything happens, so that the
  // text arriving in it is announced.
  const status = screen.getByRole('status');
  expect(status).toBeEmptyDOMElement();
  const confirm = await pickBobAndContinue();

  // An administrator transferring between two other people still manages it.
  currentDetail = {
    ...ownedDetail,
    owner: { id: 2, name: 'Bob Smith', guid: 'bob-guid' },
    manage_reason: 'admin',
  };
  await userEvent.click(confirm);

  await waitFor(() =>
    expect(fetchMock.callHistory.calls(OWNER_ENDPOINT)).toHaveLength(1),
  );
  const calls = fetchMock.callHistory.calls(OWNER_ENDPOINT);
  expect(calls[0].options.method?.toUpperCase()).toBe('PUT');
  expect(JSON.parse(calls[0].options.body as string)).toEqual({
    subject: 'user:bob-guid',
  });

  const banner = await screen.findByTestId('transfer-success');
  expect(status).toContainElement(banner);
  expect(banner).toHaveTextContent(
    'Ownership transferred to Bob Smith. You still manage this object as ' +
      'an administrator.',
  );
  expect(banner).not.toHaveTextContent('You no longer manage');
  expect(onApplied).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
  expect(fetchMock.callHistory.calls(DETAIL_ENDPOINT)).toHaveLength(2);

  // Still the owner's-eye view: the new owner is named and the drawer stays
  // fully usable.
  expect(screen.getByTestId('owner-row')).toHaveTextContent('Owner: Bob Smith');
  expect(screen.getByTestId('transfer-ownership')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Apply' })).toBeInTheDocument();
});

test('blocks Back, Cancel and Esc while the PUT is in flight', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK, { delay: 300 });
  renderDrawer();
  const confirm = await pickBobAndContinue();
  currentDetail = {
    ...ownedDetail,
    owner: { id: 2, name: 'Bob Smith', guid: 'bob-guid' },
  };
  await userEvent.click(confirm);

  expect(screen.getByTestId('sharing-drawer-back')).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  pressEscape();
  expect(onClose).not.toHaveBeenCalled();
  expect(screen.getByTestId('confirm-transfer-ownership')).toBeInTheDocument();

  // Once it has landed the drawer is back on the main view and Esc closes it
  // again, which is what proves the in-flight Esc was ignored on purpose.
  await screen.findByTestId('transfer-success');
  pressEscape();
  expect(onClose).toHaveBeenCalledTimes(1);
});

test('an administrator who owns the object is not told they are managing someone else’s', async () => {
  // The seam reports the admin ground ahead of the owner's; the drawer
  // compares the caller with the owner rather than trusting the label.
  currentDetail = {
    ...ownedDetail,
    manage_reason: 'admin',
    caller_subject: 'user:JANE-GUID',
  };
  renderDrawer();
  await screen.findByTestId('owner-row');
  expect(screen.queryByTestId('managing-as-notice')).not.toBeInTheDocument();
});

test('a tenant administrator who also holds the manage-sharing permission keeps the sharing controls', async () => {
  // The backend admits such a caller on the permission's ground for a
  // sharing write and reports can_share true; the drawer keys the
  // transfer-only mode on can_share, not on the label alone.
  currentDetail = {
    ...ownedDetail,
    visibility: 'shared',
    manage_reason: 'tenant_admin',
    can_share: true,
    caller_subject: 'user:ada-guid',
  };
  renderDrawer();
  await screen.findByTestId('managing-as-notice');
  expect(screen.getByRole('radio', { name: /Private/ })).toBeEnabled();
  expect(screen.getByRole('button', { name: /Add users & groups/ })).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Apply' })).toBeInTheDocument();
});

test('a tenant administrator sees who owns the object, may transfer it, and cannot change its sharing', async () => {
  currentDetail = {
    ...ownedDetail,
    visibility: 'shared',
    can_share: false,
    manage_reason: 'tenant_admin',
    caller_subject: 'user:ada-guid',
  };
  renderDrawer();
  const notice = await screen.findByTestId('managing-as-notice');
  expect(notice).toHaveTextContent(
    'You are not the owner of this object; Jane Doe is. As an administrator ' +
      'of this tenant you can transfer its ownership, but only its owner can ' +
      'change how it is shared. To change the sharing, take ownership first.',
  );
  // Transfer is offered, in both forms; the sharing controls are inert.
  expect(screen.getByTestId('transfer-ownership')).toBeInTheDocument();
  expect(screen.getByTestId('take-ownership')).toBeInTheDocument();
  expect(screen.getByRole('radio', { name: /Private/ })).toBeDisabled();
  expect(screen.getByRole('radio', { name: /Public/ })).toBeDisabled();
  expect(screen.getByRole('button', { name: /Add users & groups/ })).toBeDisabled();
  expect(screen.queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument();
  expect(screen.getByTestId('sharing-drawer-dismiss')).toHaveTextContent('Close');
});

test('Take ownership transfers the object to the caller and the controls go live', async () => {
  currentDetail = {
    ...ownedDetail,
    can_share: false,
    manage_reason: 'tenant_admin',
    caller_subject: 'user:ada-guid',
  };
  fetchMock.put(OWNER_ENDPOINT, { object_id: 7, owner_user_id: 9 });
  renderDrawer();
  const take = await screen.findByTestId('take-ownership');
  currentDetail = {
    ...ownedDetail,
    owner: { id: 9, name: 'Ada Lovelace', guid: 'ada-guid' },
    manage_reason: 'owner',
    caller_subject: 'user:ada-guid',
  };
  await userEvent.click(take);

  const banner = await screen.findByTestId('transfer-success');
  expect(banner).toHaveTextContent(
    'Ownership transferred to Ada Lovelace. You still manage this object as its owner.',
  );
  const calls = fetchMock.callHistory.calls(OWNER_ENDPOINT);
  expect(JSON.parse(calls[0].options.body as string)).toEqual({
    subject: 'user:ada-guid',
  });
  expect(screen.getByTestId('owner-row')).toHaveTextContent('Owner: Ada Lovelace');
  expect(screen.queryByTestId('managing-as-notice')).not.toBeInTheDocument();
  expect(screen.queryByTestId('take-ownership')).not.toBeInTheDocument();
  expect(screen.getByRole('radio', { name: /Private/ })).toBeEnabled();
  expect(onApplied).toHaveBeenCalled();
});

test('a refused Take ownership says why, in the main view', async () => {
  currentDetail = {
    ...ownedDetail,
    can_share: false,
    manage_reason: 'tenant_admin',
    caller_subject: 'user:ada-guid',
  };
  fetchMock.put(OWNER_ENDPOINT, {
    status: 409,
    body: { message: 'you do not hold access to this object’s dataset' },
  });
  renderDrawer();
  await userEvent.click(await screen.findByTestId('take-ownership'));
  expect(await screen.findByTestId('take-ownership-error')).toHaveTextContent(
    'you do not hold access to this object’s dataset',
  );
  expect(screen.getByTestId('owner-row')).toHaveTextContent('Owner: Jane Doe');
});

test('tells a Superset administrator and a manage-sharing holder the same, in their own terms', async () => {
  currentDetail = { ...ownedDetail, manage_reason: 'admin' };
  const { unmount } = renderDrawer();
  expect(await screen.findByTestId('managing-as-notice')).toHaveTextContent(
    'As a Superset administrator you can change who it is shared with',
  );
  unmount();

  currentDetail = { ...ownedDetail, manage_reason: 'manage_permission' };
  renderDrawer();
  const notice = await screen.findByTestId('managing-as-notice');
  expect(notice).toHaveTextContent('Jane Doe is');
  expect(notice).toHaveTextContent(
    'but not make it public or take ownership yourself',
  );
  expect(notice).not.toHaveTextContent('administrator');
});

test('shows no managing-as notice to the owner, to a reader, or while the object has no owner', async () => {
  currentDetail = { ...ownedDetail, manage_reason: 'owner' };
  const { unmount } = renderDrawer();
  await screen.findByTestId('owner-row');
  expect(screen.queryByTestId('managing-as-notice')).not.toBeInTheDocument();
  unmount();

  currentDetail = {
    ...ownedDetail,
    can_manage: false,
    can_share: false,
    manage_reason: null,
  };
  const second = renderDrawer();
  await screen.findByTestId('owner-row');
  expect(screen.queryByTestId('managing-as-notice')).not.toBeInTheDocument();
  second.unmount();

  currentDetail = { ...ownedDetail, owner: null, manage_reason: 'tenant_admin' };
  renderDrawer();
  await screen.findByTestId('unowned-notice');
  expect(screen.queryByTestId('managing-as-notice')).not.toBeInTheDocument();
});

test('says why the controls stay live for a tenant administrator', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK);
  renderDrawer();
  const confirm = await pickBobAndContinue();
  currentDetail = {
    ...ownedDetail,
    owner: { id: 2, name: 'Bob Smith', guid: 'bob-guid' },
    manage_reason: 'tenant_admin',
  };
  await userEvent.click(confirm);

  const banner = await screen.findByTestId('transfer-success');
  expect(banner).toHaveTextContent(
    'Ownership transferred to Bob Smith. You still manage this object as ' +
      'a tenant administrator.',
  );
  expect(screen.getByTestId('transfer-ownership')).toBeInTheDocument();
});

test('says why the controls stay live for a manage-sharing permission holder', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK);
  renderDrawer();
  const confirm = await pickBobAndContinue();
  currentDetail = {
    ...ownedDetail,
    owner: { id: 2, name: 'Bob Smith', guid: 'bob-guid' },
    manage_reason: 'manage_permission',
  };
  await userEvent.click(confirm);

  const banner = await screen.findByTestId('transfer-success');
  expect(banner).toHaveTextContent(
    'Ownership transferred to Bob Smith. You still manage sharing for this ' +
      'object through your sharing-management permission: you can share it ' +
      'or transfer it to someone else, but not make it public or take ' +
      'ownership yourself.',
  );
  expect(banner).not.toHaveTextContent('as its owner');
  expect(banner).not.toHaveTextContent('administrator');
  // A third-party transfer is what the permission allows, so the link stays.
  expect(screen.getByTestId('transfer-ownership')).toBeInTheDocument();
});

test('never offers Assign owner to me on the strength of the manage-sharing permission', async () => {
  // The backend refuses a claim under that ground; a detail that reports it
  // together with a missing or deactivated owner gets no claim button.
  currentDetail = {
    ...ownedDetail,
    owner: null,
    can_share: false,
    manage_reason: 'manage_permission',
  };
  renderDrawer();
  await screen.findByTestId('unowned-notice');
  expect(screen.queryByTestId('assign-owner-to-me')).not.toBeInTheDocument();
  expect(screen.queryByTestId('transfer-ownership')).not.toBeInTheDocument();
});

test('gives up on a PUT that never answers and says so', async () => {
  // What the client does with a timeout: the fetch is raced against a
  // rejection, so a hung socket rejects with statusText 'timeout'. Modelled
  // here with a short delay in place of the real 30 s.
  const put = jest.spyOn(SupersetClient, 'put').mockImplementationOnce(
    ({ timeout }) =>
      new Promise((_, reject) => {
        setTimeout(() => reject(timeoutRejection(timeout as number)), 300);
      }),
  );
  renderDrawer();
  const confirm = await pickBobAndContinue();
  await userEvent.click(confirm);

  expect(put).toHaveBeenCalledWith(
    expect.objectContaining({ timeout: ASSIGN_OWNER_TIMEOUT_MS }),
  );
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();

  const error = await screen.findByRole('alert');
  expect(error).toHaveTextContent('The server did not answer in time');
  expect(error).toHaveTextContent('Check the current owner below');
  // The write may have landed; the drawer must not claim otherwise.
  expect(error).not.toHaveTextContent('Nothing has been changed');
  // The exits are back.
  expect(screen.getByTestId('sharing-drawer-back')).toBeEnabled();
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeEnabled();
  // The detail is read again; with the owner unchanged, a retry is safe and
  // the confirm button comes back once the re-read has answered.
  await waitFor(() =>
    expect(fetchMock.callHistory.calls(DETAIL_ENDPOINT)).toHaveLength(2),
  );
  await waitFor(() =>
    expect(screen.getByTestId('confirm-transfer-ownership')).toBeEnabled(),
  );
  expect(screen.getByTestId('transfer-summary')).toHaveTextContent(
    'from Jane Doe to Bob Smith?',
  );
  pressEscape();
  expect(onClose).toHaveBeenCalledTimes(1);
  // And the list re-reads, since its rows may have changed.
  expect(onApplied).toHaveBeenCalledTimes(1);
});

test('re-reads after a timeout and follows what it finds', async () => {
  // The write landed after all, and the caller was its owner: the re-read
  // names the new owner and reports can_manage false.
  jest.spyOn(SupersetClient, 'put').mockImplementationOnce(
    ({ timeout }) =>
      new Promise((_, reject) => {
        setTimeout(() => reject(timeoutRejection(timeout as number)), 300);
      }),
  );
  renderDrawer();
  const confirm = await pickBobAndContinue();
  currentDetail = {
    ...ownedDetail,
    owner: { id: 2, name: 'Bob Smith', guid: 'bob-guid' },
    can_manage: false,
    can_share: false,
  };
  // The re-read takes a moment, so that its in-flight state can be seen.
  const realGet = SupersetClient.get.bind(SupersetClient);
  jest.spyOn(SupersetClient, 'get').mockImplementationOnce(async request => {
    await new Promise(resolve => {
      setTimeout(resolve, 300);
    });
    return realGet(request);
  });
  await userEvent.click(confirm);

  // Inert while the re-read is in flight, and still inert once it has
  // answered: the caller no longer manages the object.
  await screen.findByRole('alert');
  expect(fetchMock.callHistory.calls(DETAIL_ENDPOINT)).toHaveLength(1);
  expect(screen.getByTestId('confirm-transfer-ownership')).toBeDisabled();
  await waitFor(() =>
    expect(fetchMock.callHistory.calls(DETAIL_ENDPOINT)).toHaveLength(2),
  );
  await waitFor(() =>
    expect(screen.getByTestId('transfer-summary')).toHaveTextContent(
      'from Bob Smith to Bob Smith?',
    ),
  );
  expect(screen.getByTestId('confirm-transfer-ownership')).toBeDisabled();

  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await screen.findByTestId('owner-row');
  expect(screen.getByTestId('owner-row')).toHaveTextContent('Owner: Bob Smith');
  expectReadOnly();
});

test('downgrades to read-only after transferring away', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK);
  renderDrawer();
  const confirm = await pickBobAndContinue();

  // A public object: the previous owner can still read it, but no longer
  // manage it.
  currentDetail = {
    ...ownedDetail,
    owner: { id: 2, name: 'Bob Smith' },
    visibility: 'public',
    can_manage: false,
    can_share: false,
  };
  await userEvent.click(confirm);

  const banner = await screen.findByTestId('transfer-success');
  expect(banner).toHaveTextContent('Ownership transferred to Bob Smith.');
  expect(banner).toHaveTextContent('You no longer manage this object');
  expect(onApplied).toHaveBeenCalledTimes(1);

  expectReadOnly();
  screen.getAllByRole('radio').forEach(radio => expect(radio).toBeDisabled());
});

test.each([403, 404])(
  'reports lost access when the re-read answers %s',
  async status => {
    fetchMock.put(OWNER_ENDPOINT, PUT_OK);
    renderDrawer();
    const confirm = await pickBobAndContinue();

    // A private object: its previous owner is told it is not there (404),
    // or is refused outright (403).
    currentDetail = status;
    await userEvent.click(confirm);

    const banner = await screen.findByTestId('transfer-success');
    expect(screen.getByRole('status')).toContainElement(banner);
    expect(banner).toHaveTextContent('Ownership transferred to Bob Smith.');
    expect(banner).toHaveTextContent('You no longer manage this object');
    expect(screen.queryByTestId('ownership-error')).not.toBeInTheDocument();
    // Nothing was read back, so there is no owner row or radio to show --
    // the object name and the banner are what is left.
    expect(screen.queryByTestId('owner-row')).not.toBeInTheDocument();
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    expectReadOnly();
    expect(onApplied).toHaveBeenCalledTimes(1);
  },
);

test('does not claim lost access when the re-read fails with 500', async () => {
  fetchMock.put(OWNER_ENDPOINT, PUT_OK);
  renderDrawer();
  const confirm = await pickBobAndContinue();

  currentDetail = 500;
  await userEvent.click(confirm);

  const banner = await screen.findByTestId('transfer-success');
  expect(screen.getByRole('status')).toContainElement(banner);
  expect(banner).toHaveTextContent(
    'Ownership was transferred to Bob Smith; the latest details could not ' +
      'be loaded.',
  );
  expect(banner).not.toHaveTextContent('You no longer manage');
  expect(screen.queryByTestId('ownership-error')).not.toBeInTheDocument();
  // Fails closed all the same: nothing that writes is offered.
  expect(screen.queryByTestId('owner-row')).not.toBeInTheDocument();
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  expectReadOnly();
  expect(onApplied).toHaveBeenCalledTimes(1);
});

test('shows the API message verbatim when the PUT is refused', async () => {
  fetchMock.put(OWNER_ENDPOINT, {
    status: 409,
    body: {
      message: "Bob Smith does not have access to this object's dataset",
    },
  });
  renderDrawer();
  const confirm = await pickBobAndContinue();
  await userEvent.click(confirm);

  const error = await screen.findByRole('alert');
  expect(error).toHaveAttribute('data-test', 'ownership-error');
  expect(
    within(error).getByText(
      "Bob Smith does not have access to this object's dataset",
    ),
  ).toBeInTheDocument();
  // Still on the confirmation step, nothing refreshed, nothing closed.
  expect(screen.getByTestId('confirm-transfer-ownership')).toBeEnabled();
  expect(onApplied).not.toHaveBeenCalled();
  expect(onClose).not.toHaveBeenCalled();
  expect(fetchMock.callHistory.calls(DETAIL_ENDPOINT)).toHaveLength(1);
});

test('re-reads the detail after a 403 so the stale link goes', async () => {
  fetchMock.put(OWNER_ENDPOINT, {
    status: 403,
    body: { message: 'only the owner may assign ownership' },
  });
  renderDrawer();
  const confirm = await pickBobAndContinue();

  // The caller lost can_manage between opening the drawer and confirming.
  currentDetail = { ...ownedDetail, can_manage: false, can_share: false };
  await userEvent.click(confirm);

  const error = await screen.findByRole('alert');
  expect(error).toHaveTextContent('only the owner may assign ownership');
  await waitFor(() =>
    expect(fetchMock.callHistory.calls(DETAIL_ENDPOINT)).toHaveLength(2),
  );
  await waitFor(() =>
    expect(screen.getByTestId('confirm-transfer-ownership')).toBeDisabled(),
  );
  expect(onApplied).not.toHaveBeenCalled();

  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await screen.findByTestId('owner-row');
  expectReadOnly();
});

test('fails closed when the re-read after a 403 is refused', async () => {
  fetchMock.put(OWNER_ENDPOINT, {
    status: 403,
    body: { message: 'only the owner may assign ownership' },
  });
  renderDrawer();
  const confirm = await pickBobAndContinue();

  // Someone else transferred the (non-public) object away while the drawer
  // was open: the PUT is refused, and so is the re-read.
  currentDetail = 404;
  await userEvent.click(confirm);

  await waitFor(() =>
    expect(fetchMock.callHistory.calls(DETAIL_ENDPOINT)).toHaveLength(2),
  );
  // There is nothing left to confirm against, so the confirmation is gone
  // and the main view offers nothing that writes: no owner row, no radio,
  // no Transfer link, no Apply -- and no claim that nothing changed.
  await waitFor(() =>
    expect(
      screen.queryByTestId('confirm-transfer-ownership'),
    ).not.toBeInTheDocument(),
  );
  const error = screen.getByTestId('ownership-error');
  expect(error).toHaveTextContent('Could not read this object’s sharing');
  expect(error).not.toHaveTextContent('Nothing has been changed');
  expect(
    screen.getByRole('heading', { name: 'Quarterly revenue' }),
  ).toHaveFocus();
  expect(screen.queryByTestId('owner-row')).not.toBeInTheDocument();
  expect(screen.queryByRole('radio')).not.toBeInTheDocument();
  expectReadOnly();
  expect(onApplied).not.toHaveBeenCalled();
  // No way back into the transfer; only out.
  await userEvent.click(screen.getByTestId('sharing-drawer-dismiss'));
  expect(onClose).toHaveBeenCalledTimes(1);
});

test('disables Transfer ownership while changes are pending', async () => {
  renderDrawer();
  await screen.findByTestId('owner-row');
  expect(screen.getByTestId('transfer-ownership')).toBeEnabled();

  await userEvent.click(screen.getByRole('radio', { name: /Public/ }));
  const link = screen.getByTestId('transfer-ownership');
  expect(link).toBeDisabled();
  expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();

  await userEvent.hover(link);
  expect(
    await screen.findByText('Apply or discard your pending changes first'),
  ).toBeInTheDocument();

  await userEvent.click(screen.getByRole('radio', { name: /Private/ }));
  expect(screen.getByTestId('transfer-ownership')).toBeEnabled();
});

test('the share picker: both kinds, checkboxes, emails shown', async () => {
  currentDetail = { ...ownedDetail, visibility: 'shared' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));

  // Both kinds, minus the owner (they already have full access and are
  // named in the drawer's header), and the same name-over-email rows as
  // the transfer picker.
  const bob = await screen.findByRole('checkbox', { name: 'Bob Smith' });
  expect(
    within(bob.closest('div') as HTMLElement).getByTestId('subject-detail'),
  ).toHaveTextContent('bob@example.com');
  expect(screen.getByText(/Groups \(1\)/)).toBeInTheDocument();
  expect(screen.getByTestId('subject-picker-ok')).toBeEnabled();

  // Checks accumulate, and the selection comes back as viewer shares.
  await userEvent.click(bob);
  await userEvent.click(screen.getByRole('checkbox', { name: 'Carol Jones' }));
  expect(screen.getByTestId('subject-picker-ok')).toHaveTextContent(
    'Confirm (2)',
  );
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  expect(await screen.findByText('Shared with 2 users')).toBeInTheDocument();
});

test('the share picker never offers the owner', async () => {
  currentDetail = { ...ownedDetail, visibility: 'shared' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));

  await screen.findByRole('checkbox', { name: 'Bob Smith' });
  // Jane owns the object: not a row, not a count. The header still names her.
  expect(
    screen.queryByRole('checkbox', { name: 'Jane Doe' }),
  ).not.toBeInTheDocument();
  expect(screen.getByText(/Users \(2\)/)).toBeInTheDocument();
  // Same rule as the transfer picker when the guid's case differs.
  currentSubjects = {
    result: subjects.map(o =>
      o.value === 'user:jane-guid'
        ? { ...o, value: 'user:JANE-GUID', extra: { ...o.extra, guid: 'JANE-GUID' } }
        : o,
    ),
    tenant: 'acme',
  };
  await userEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await userEvent.click(screen.getByText('Add users & groups'));
  await screen.findByRole('checkbox', { name: 'Bob Smith' });
  // The re-open fetched /subjects again, so the variant rows were the ones
  // judged.
  expect(fetchMock.callHistory.calls(SUBJECTS_ENDPOINT)).toHaveLength(2);
  expect(
    screen.queryByRole('checkbox', { name: 'Jane Doe' }),
  ).not.toBeInTheDocument();
});

test('a stale owner share is offered on a local instance too (owner matched by id)', async () => {
  // Local instance: the owner's guid ("admin") never equals the store's
  // spelling of the same account ("local-1"), so the owner is recognised
  // by Superset id. The stale share is spelled the store's way; it must
  // still be offered so it can be removed.
  fetchMock.delete(SHARE_DELETE_ENDPOINT, {});
  currentDetail = {
    ...ownedDetail,
    visibility: 'shared',
    owner: { id: 1, name: 'Jane Doe', guid: 'admin' },
    shares: [{ subject: 'user:local-1', name: 'local-1', role: 'viewer' }],
  };
  currentSubjects = {
    tenant: 'acme',
    result: [
      {
        value: 'user:local-1',
        text: 'local-1',
        extra: { type: 'user', guid: 'local-1', id: 1, email: null },
      },
      ...subjects.slice(1),
    ],
  };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));

  const own = await screen.findByRole('checkbox', { name: 'local-1' });
  expect(own).toBeChecked();
  expect(screen.getByTestId('subject-picker-ok')).toHaveTextContent(
    'Confirm (1)',
  );
  await userEvent.click(own);
  // Reverting before Apply is still possible: the row does not vanish.
  expect(screen.getByRole('checkbox', { name: 'local-1' })).not.toBeChecked();
  await userEvent.click(screen.getByRole('checkbox', { name: 'local-1' }));
  expect(screen.getByRole('checkbox', { name: 'local-1' })).toBeChecked();
  await userEvent.click(screen.getByRole('checkbox', { name: 'local-1' }));
  expect(screen.getByTestId('subject-picker-ok')).toHaveTextContent('Confirm');
  expect(screen.getByTestId('subject-picker-ok')).not.toHaveTextContent('(');
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  const calls = fetchMock.callHistory.calls(SHARE_DELETE_ENDPOINT);
  expect(calls.map(c => decodeURIComponent(c.url.split('/shares/')[1]))).toEqual(
    ['user:local-1'],
  );
});

test('a stale share naming the owner stays offered so it can be removed', async () => {
  // A transfer leaves the new owner's earlier share in place: Jane now owns
  // the object AND still has a viewer share. Hiding her row would leave a
  // share that is counted but cannot be removed from the drawer.
  fetchMock.delete(SHARE_DELETE_ENDPOINT, {});
  currentDetail = {
    ...ownedDetail,
    visibility: 'shared',
    shares: [
      { subject: 'user:jane-guid', name: 'Jane Doe', role: 'viewer' },
      { subject: 'user:bob-guid', name: 'Bob Smith', role: 'viewer' },
    ],
  };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));

  const jane = await screen.findByRole('checkbox', { name: 'Jane Doe' });
  expect(jane).toBeChecked();
  expect(screen.getByText(/Users \(3\)/)).toBeInTheDocument();
  expect(screen.getByTestId('subject-picker-ok')).toHaveTextContent(
    'Confirm (2)',
  );
  await userEvent.click(jane);
  expect(screen.getByTestId('subject-picker-ok')).toHaveTextContent(
    'Confirm (1)',
  );
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  expect(await screen.findByText('Shared with 1 users')).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  const calls = fetchMock.callHistory.calls(SHARE_DELETE_ENDPOINT);
  expect(calls.map(c => decodeURIComponent(c.url.split('/shares/')[1]))).toEqual(
    ['user:jane-guid'],
  );
});

test('a re-selected share keeps its role; a new pick is a viewer', async () => {
  fetchMock.post(SHARES_ENDPOINT, {});
  currentDetail = {
    ...ownedDetail,
    visibility: 'shared',
    shares: [{ subject: 'user:bob-guid', name: 'Bob Smith', role: 'editor' }],
  };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));

  // Bob is already checked; adding Carol hands back both.
  expect(
    await screen.findByRole('checkbox', { name: 'Bob Smith' }),
  ).toBeChecked();
  await userEvent.click(screen.getByRole('checkbox', { name: 'Carol Jones' }));
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  expect(await screen.findByText('Shared with 2 users')).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));

  // One write, for the newcomer, as a viewer; Bob's editor share is untouched.
  const calls = fetchMock.callHistory.calls(SHARES_ENDPOINT);
  expect(calls).toHaveLength(1);
  expect(JSON.parse(calls[0].options.body as string)).toEqual({
    subject: 'user:carol-guid',
    role: 'viewer',
  });
  expect(fetchMock.callHistory.calls(OWNER_ENDPOINT)).toHaveLength(0);
});

test('the Private option says existing shares are removed', async () => {
  // Issue #93: the backend now revokes every existing share the moment an
  // object goes private, so the drawer's own copy has to stop promising
  // that a previously shared object stays readable by everyone it was
  // shared with.
  renderDrawer();
  await screen.findByTestId('owner-row');
  expect(
    screen.getByText('Visible to only you. Existing shares are removed.'),
  ).toBeInTheDocument();
});

test('Apply on shared -> private sends only the visibility PUT; the backend revokes shares', async () => {
  // The frontend used to have to issue removeShare for every existing share
  // to close a shared object; that call has moved server-side
  // (_set_asset_visibility queues the revocations itself), so Apply must
  // send the single visibility PUT and nothing against /shares.
  currentDetail = {
    ...ownedDetail,
    visibility: 'shared',
    shares: [
      { subject: 'user:bob-guid', name: 'Bob Smith', role: 'viewer' },
      { subject: 'group:designers#member', name: 'Designers', role: 'viewer' },
    ],
  };
  fetchMock.put(VISIBILITY_ENDPOINT, { object_id: 7, visibility: 'private' });
  renderDrawer();
  await screen.findByTestId('owner-row');

  await userEvent.click(screen.getByRole('radio', { name: /Private/ }));
  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));

  expect(fetchMock.callHistory.calls(VISIBILITY_ENDPOINT)).toHaveLength(1);
  expect(fetchMock.callHistory.calls(SHARES_ENDPOINT)).toHaveLength(0);
  expect(fetchMock.callHistory.calls(SHARE_DELETE_ENDPOINT)).toHaveLength(0);
});

// --- A holder of the manage-sharing permission ------------------------------

const holderDetail: OwnershipDetail = {
  ...ownedDetail,
  visibility: 'shared',
  manage_reason: 'manage_permission',
  // The caller is Bob: shared with as a viewer, and not the owner.
  caller_subject: 'user:bob-guid',
  shares: [
    { subject: 'user:bob-guid', name: 'Bob Smith', role: 'viewer' },
    { subject: 'user:carol-guid', name: 'Carol Jones', role: 'viewer' },
  ],
};

test('disables Public, and says why, for a manage-sharing permission holder', async () => {
  // The backend refuses `-> public` under that permission (403), so the
  // option is not offered; the other two stay live and Apply is there.
  currentDetail = holderDetail;
  renderDrawer();
  await screen.findByTestId('owner-row');

  const publicRadio = screen.getByRole('radio', { name: /Public/ });
  expect(publicRadio).toBeDisabled();
  expect(
    screen.getByText(
      'Shared with users in your organisation. Only the owner or an administrator can make this object public.',
    ),
  ).toBeInTheDocument();
  expect(screen.getByRole('radio', { name: /Private/ })).toBeEnabled();
  expect(screen.getByRole('radio', { name: /^Shared/ })).toBeEnabled();
  expect(screen.getByRole('radio', { name: /^Shared/ })).toBeChecked();

  await userEvent.click(publicRadio);
  expect(publicRadio).not.toBeChecked();
  expect(screen.getByRole('button', { name: 'Apply' })).toBeDisabled();

  await userEvent.click(screen.getByRole('radio', { name: /Private/ }));
  expect(screen.getByRole('button', { name: 'Apply' })).toBeEnabled();
});

test('offers Public, without the explanation, to an owner', async () => {
  currentDetail = { ...ownedDetail, manage_reason: 'owner' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  expect(screen.getByRole('radio', { name: /Public/ })).toBeEnabled();
  expect(
    screen.queryByText(/Only the owner or an administrator can make/),
  ).not.toBeInTheDocument();
});

test('shows the API reason when Apply is refused', async () => {
  fetchMock.put(VISIBILITY_ENDPOINT, {
    status: 403,
    body: {
      message:
        'the manage-sharing permission does not include making an object public',
    },
  });
  currentDetail = { ...ownedDetail, manage_reason: 'owner' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByRole('radio', { name: /Public/ }));
  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));

  const error = await screen.findByRole('alert');
  expect(error).toHaveTextContent(
    'Could not save these changes: the manage-sharing permission does not ' +
      'include making an object public. Reopen this panel to see the ' +
      'current settings.',
  );
  // Open, re-read requested by the list, nothing closed.
  expect(onApplied).toHaveBeenCalledTimes(1);
  expect(onClose).not.toHaveBeenCalled();
});

test('keeps the generic line when the refusal carries no message', async () => {
  fetchMock.put(VISIBILITY_ENDPOINT, { status: 500, body: {} });
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByRole('radio', { name: /Public/ }));
  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));

  expect(await screen.findByRole('alert')).toHaveTextContent(
    'Could not save these changes. Reopen this panel to see the current settings.',
  );
});

test("locks the holder's own row in the share picker", async () => {
  // The backend refuses a change to the caller's own share under the
  // permission; the row is shown, checked and inert, with the reason. Other
  // rows are the holder's to change, and Apply writes only those.
  fetchMock.delete(SHARE_DELETE_ENDPOINT, {});
  currentDetail = holderDetail;
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));

  const bob = await screen.findByRole('checkbox', { name: 'Bob Smith' });
  expect(bob).toBeChecked();
  expect(bob).toBeDisabled();
  expect(
    within(bob.closest('div') as HTMLElement).getByTestId('subject-locked'),
  ).toHaveTextContent(
    'Your own share can only be changed by the owner or an administrator.',
  );
  const carol = screen.getByRole('checkbox', { name: 'Carol Jones' });
  expect(carol).toBeChecked();
  expect(carol).toBeEnabled();
  // The owner (Jane) is never a row, for a holder either.
  expect(
    screen.queryByRole('checkbox', { name: 'Jane Doe' }),
  ).not.toBeInTheDocument();

  await userEvent.click(carol);
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  expect(await screen.findByText('Shared with 1 users')).toBeInTheDocument();

  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  const calls = fetchMock.callHistory.calls(SHARE_DELETE_ENDPOINT);
  expect(calls.map(c => decodeURIComponent(c.url.split('/shares/')[1]))).toEqual(
    ['user:carol-guid'],
  );
});

test("locks the tenant's structural groups for a holder", async () => {
  // The backend refuses a share to, or an unshare of, the tenant's own
  // membership or administrator group under the permission -- that is the
  // whole tenant, not a third party. /subjects says which rows those are;
  // the drawer locks them with the reason and leaves an ordinary group
  // (Designers) to the holder. Apply writes only the row the holder changed.
  fetchMock.post(SHARES_ENDPOINT, {});
  currentSubjects = {
    result: [
      ...subjects,
      {
        value: 'group:tenant_administrator_acme#member',
        text: 'tenant administrator',
        extra: {
          type: 'group',
          members: 2,
          in_superset: false,
          structural: true,
        },
      },
      {
        value: 'group:tenant_acme#member',
        text: 'tenant',
        extra: {
          type: 'group',
          members: 9,
          in_superset: false,
          structural: true,
        },
      },
    ],
    tenant: 'acme',
  };
  currentDetail = {
    ...holderDetail,
    shares: [
      ...holderDetail.shares,
      { subject: 'group:tenant_acme#member', name: 'tenant', role: 'viewer' },
    ],
  };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));
  await screen.findByRole('checkbox', { name: 'Bob Smith' });
  await userEvent.click(screen.getByText(/Groups \(3\)/));

  const admins = await screen.findByRole('checkbox', {
    name: 'tenant administrator',
  });
  expect(admins).not.toBeChecked();
  expect(admins).toBeDisabled();
  expect(
    within(admins.closest('div') as HTMLElement).getByTestId('subject-locked'),
  ).toHaveTextContent(
    'The whole tenant can only be shared by the owner or an administrator.',
  );
  // A structural group the owner already shared to stays listed, checked
  // and inert: the holder cannot undo a tenant-wide share either.
  const tenant = screen.getByRole('checkbox', { name: 'tenant' });
  expect(tenant).toBeChecked();
  expect(tenant).toBeDisabled();
  const designers = screen.getByRole('checkbox', { name: 'Designers' });
  expect(designers).toBeEnabled();
  expect(screen.getAllByTestId('subject-locked')).toHaveLength(2);

  await userEvent.click(designers);
  await userEvent.click(screen.getByTestId('subject-picker-ok'));
  await userEvent.click(screen.getByRole('button', { name: 'Apply' }));
  await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  const posted = fetchMock.callHistory
    .calls(SHARES_ENDPOINT)
    .map(c => JSON.parse(c.options.body as string).subject);
  expect(posted).toEqual(['group:designers#member']);
});

test('offers the structural groups to an owner', async () => {
  currentSubjects = {
    result: [
      ...subjects,
      {
        value: 'group:tenant_administrator_acme#member',
        text: 'tenant administrator',
        extra: {
          type: 'group',
          members: 2,
          in_superset: false,
          structural: true,
        },
      },
    ],
    tenant: 'acme',
  };
  currentDetail = { ...holderDetail, manage_reason: 'owner' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));
  await screen.findByRole('checkbox', { name: 'Bob Smith' });
  await userEvent.click(screen.getByText(/Groups \(2\)/));
  expect(
    await screen.findByRole('checkbox', { name: 'tenant administrator' }),
  ).toBeEnabled();
  expect(screen.queryByTestId('subject-locked')).not.toBeInTheDocument();
});

test('locks no group when the backend does not flag one', async () => {
  // An older /subjects reports no `structural`: nothing to lock, and the
  // API's refusal is what the holder sees on Apply.
  currentSubjects = {
    result: [
      ...subjects,
      {
        value: 'group:tenant_administrator_acme#member',
        text: 'tenant administrator',
        extra: { type: 'group', members: 2, in_superset: false },
      },
    ],
    tenant: 'acme',
  };
  currentDetail = holderDetail;
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));
  // The holder's own row is locked on the users tab...
  expect(
    await screen.findByRole('checkbox', { name: 'Bob Smith' }),
  ).toBeDisabled();
  await userEvent.click(screen.getByText(/Groups \(2\)/));
  // ...and no group is, since nothing says which is structural.
  expect(
    await screen.findByRole('checkbox', { name: 'tenant administrator' }),
  ).toBeEnabled();
  expect(screen.queryByTestId('subject-locked')).not.toBeInTheDocument();
});

test("compares the holder's subject case-insensitively", async () => {
  currentDetail = { ...holderDetail, caller_subject: 'user:BOB-GUID' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));
  expect(
    await screen.findByRole('checkbox', { name: 'Bob Smith' }),
  ).toBeDisabled();
});

test('locks no row for an owner', async () => {
  currentDetail = { ...holderDetail, manage_reason: 'owner' };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));
  expect(
    await screen.findByRole('checkbox', { name: 'Bob Smith' }),
  ).toBeEnabled();
  expect(screen.queryByTestId('subject-locked')).not.toBeInTheDocument();
});

test('locks no row when the backend does not say who the caller is', async () => {
  // An older backend reports no caller_subject: nothing to lock, and the
  // API's refusal is what the caller sees on Apply.
  currentDetail = { ...holderDetail, caller_subject: undefined };
  renderDrawer();
  await screen.findByTestId('owner-row');
  await userEvent.click(screen.getByText('Add users & groups'));
  expect(
    await screen.findByRole('checkbox', { name: 'Bob Smith' }),
  ).toBeEnabled();
  expect(screen.queryByTestId('subject-locked')).not.toBeInTheDocument();
});
