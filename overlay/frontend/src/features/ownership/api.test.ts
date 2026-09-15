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

import { sharingActionTooltip, unknownOwnership } from './api';
import type { OwnershipListItem } from './types';

const managed: OwnershipListItem = {
  object_id: 1,
  owner: { id: 1, name: 'Jane Doe' },
  visibility: 'private',
  unowned: false,
  can_manage: true,
  can_share: true,
};

const notManaged: OwnershipListItem = {
  ...managed,
  can_manage: false,
  can_share: false,
};

// F-1 (test-cases/ivanti-acceptance-run.md section 4): the disabled Sharing
// action must not blame the caller's permissions when the real reason is
// that ownership state could not be read at all.

test('a manager sees the plain "Sharing" tooltip', () => {
  expect(sharingActionTooltip(managed)).toBe('Sharing');
});

test('a real non-manager keeps the permission-denied tooltip', () => {
  expect(sharingActionTooltip(notManaged)).toBe(
    'You do not manage this object, so it cannot be shared from here.',
  );
});

test('a row whose ownership state is unknown (the list endpoint failed) gets the outage tooltip', () => {
  // The exact shape a missing row is backfilled with.
  expect(sharingActionTooltip(unknownOwnership(7))).toBe(
    'Sharing is temporarily unavailable: the authorization store cannot be reached.',
  );
});

test('a disabled (parked) row keeps the permission-denied tooltip, not the outage one', () => {
  expect(
    sharingActionTooltip({ ...notManaged, visibility: null, disabled: true }),
  ).toBe('You do not manage this object, so it cannot be shared from here.');
});
