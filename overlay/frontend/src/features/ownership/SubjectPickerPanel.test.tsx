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
import { render, screen } from 'spec/helpers/testing-library';
import SubjectPickerPanel, { rowLabels } from './SubjectPickerPanel';
import type { OwnershipSubjectOption } from './types';

const SUBJECTS_ENDPOINT = 'glob:*/api/v1/ownership/subjects?*';

// D-5 (qa/design/directory-hook/02-technical-spec.md section 7.3): the
// directory's fast group listing path (the additive group.tenant relation)
// answers a group's membership count as null, not zero -- a zero would read
// as an empty group, which the fast path cannot tell from "not counted".
// rowLabels is the one place that distinction reaches the picker's copy.

function groupOption(members: number | null | undefined): OwnershipSubjectOption {
  return {
    value: 'group:dashboard_designer_tenant#member',
    text: 'dashboard designer',
    extra: { type: 'group', members },
  };
}

test('a group with a known member count shows the count', () => {
  expect(rowLabels(groupOption(3)).secondary).toBe('3 member(s) · from directory');
});

test('a group with a zero member count still shows the count, not "from directory"', () => {
  expect(rowLabels(groupOption(0)).secondary).toBe('0 member(s) · from directory');
});

test('a group with no counted membership (the directory fast path) shows "from directory" with no count', () => {
  expect(rowLabels(groupOption(null)).secondary).toBe('from directory');
});

test('a group option with the field entirely absent (an older backend) also reads as uncounted', () => {
  expect(rowLabels(groupOption(undefined)).secondary).toBe('from directory');
});

test('a user option is unaffected by the group member-count copy', () => {
  const user: OwnershipSubjectOption = {
    value: 'user:3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31',
    text: 'Ada Lovelace',
    extra: {
      type: 'user',
      guid: '3f0a91c7-2d84-4e63-9b15-7c4e8a2f6d31',
      email: 'ada@example.com',
      id: 1,
    },
  };
  expect(rowLabels(user)).toEqual({ primary: 'Ada Lovelace', secondary: 'ada@example.com' });
});

// F-5 (test-cases/ivanti-acceptance-run.md section 4): a member Superset has
// no display name for is labelled by the best name available -- their email
// when there is one -- never a bare, unformatted GUID. The share and
// transfer pickers both render rows through this same function, so getting
// it right here fixes both at once.
test('an unnamed user with a GUID-derived email is labelled by that email, not a bare GUID', () => {
  const ben: OwnershipSubjectOption = {
    value: 'user:6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53',
    // /subjects falls back to the GUID itself when Superset has no name.
    text: '6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53',
    extra: {
      type: 'user',
      guid: '6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53',
      email: '6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53@ivanti.example',
      id: 2,
    },
  };
  expect(rowLabels(ben)).toEqual({
    primary: '6c2b48e9-5a71-4f92-8d03-2e9b7c1a4d53@ivanti.example',
    secondary: null,
  });
});

afterEach(() => {
  fetchMock.clearHistory().removeRoutes();
});

const renderPanel = () =>
  render(
    <SubjectPickerPanel
      initiallySelected={[]}
      onCancel={jest.fn()}
      onOk={jest.fn()}
    />,
  );

// F-2 (test-cases/ivanti-acceptance-run.md section 4): /subjects can answer
// 200 with `degraded: true` when the directory or authorization store could
// not be fully consulted -- an empty `result` in that case means "could not
// tell", not "nobody matched", and the picker must say so rather than
// implying the directory is empty.
test('a degraded /subjects response explains the outage instead of "No results found"', async () => {
  fetchMock.get(SUBJECTS_ENDPOINT, {
    result: [],
    tenant: 'acme',
    degraded: true,
  });
  renderPanel();
  expect(
    await screen.findByText(
      'The directory is temporarily unavailable. Try again in a moment.',
    ),
  ).toBeInTheDocument();
  expect(screen.queryByText('No results found')).not.toBeInTheDocument();
});

test('a non-degraded empty /subjects response still reads as "No results found"', async () => {
  fetchMock.get(SUBJECTS_ENDPOINT, { result: [], tenant: 'acme' });
  renderPanel();
  expect(await screen.findByText('No results found')).toBeInTheDocument();
});

// Issue #125: a Superset admin has no tenant of their own, so /subjects
// answered with nothing and the picker was permanently empty for the one
// caller with authority over every object. The panel names the object it is
// picking for, and the route falls back to that object's tenant.
test('the object being shared is sent to /subjects', async () => {
  fetchMock.get(SUBJECTS_ENDPOINT, { result: [], tenant: 'acme' });
  render(
    <SubjectPickerPanel
      initiallySelected={[]}
      object="chart:12"
      onCancel={jest.fn()}
      onOk={jest.fn()}
    />,
  );
  await screen.findByText('No results found');
  const calls = fetchMock.callHistory.calls(SUBJECTS_ENDPOINT);
  expect(calls[calls.length - 1].url).toContain('object=chart%3A12');
});

test('no object means no parameter, for a caller scoped by their own tenant', async () => {
  fetchMock.get(SUBJECTS_ENDPOINT, { result: [], tenant: 'acme' });
  renderPanel();
  await screen.findByText('No results found');
  const calls = fetchMock.callHistory.calls(SUBJECTS_ENDPOINT);
  expect(calls[calls.length - 1].url).not.toContain('object=');
});
