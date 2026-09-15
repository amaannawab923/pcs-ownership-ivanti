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
// Imported first: loading this before 'spec/helpers/testing-library' or
// '@superset-ui/core' ensures mockAntdWithDesktopBreakpoint is defined
// before anything transitively requires (and thus mocks) 'antd'.
import { mockAntdWithDesktopBreakpoint } from 'spec/helpers/mobileTestUtils';
import fetchMock from 'fetch-mock';
import { isFeatureEnabled } from '@superset-ui/core';
import { mockUserSubjectsBootstrapData } from 'spec/helpers/mockBootstrapData';
import {
  screen,
  selectPillOption,
  waitFor,
  fireEvent,
  within,
} from 'spec/helpers/testing-library';
import {
  mockDashboards,
  mockAdminUser,
  setupMocks,
  renderDashboardList,
  API_ENDPOINTS,
  getLatestDashboardApiCall,
} from './DashboardList.testHelpers';

jest.setTimeout(30000);

jest.mock('@superset-ui/core', () => ({
  ...jest.requireActual('@superset-ui/core'),
  isFeatureEnabled: jest.fn(),
}));

jest.mock('src/utils/export', () => ({
  __esModule: true,
  default: jest.fn(),
}));

jest.mock('src/utils/getBootstrapData', () =>
  mockUserSubjectsBootstrapData([1]),
);

// Mock useBreakpoint to return desktop breakpoints (prevents mobile rendering)
jest.mock('antd', () => mockAntdWithDesktopBreakpoint());

const mockIsFeatureEnabled = isFeatureEnabled as jest.MockedFunction<
  typeof isFeatureEnabled
>;

beforeEach(() => {
  setupMocks();
  mockIsFeatureEnabled.mockImplementation(
    (feature: string) => feature === 'LISTVIEWS_DEFAULT_CARD_VIEW',
  );
});

afterEach(() => {
  fetchMock.clearHistory().removeRoutes();
  mockIsFeatureEnabled.mockReset();
});

test('renders', async () => {
  renderDashboardList(mockAdminUser);
  expect(await screen.findByText('Dashboards')).toBeInTheDocument();
});

test('renders a ListView', async () => {
  renderDashboardList(mockAdminUser);
  expect(await screen.findByTestId('dashboard-list-view')).toBeInTheDocument();
});

test('fetches info', async () => {
  renderDashboardList(mockAdminUser);
  await waitFor(() => {
    const calls = fetchMock.callHistory.calls(/dashboard\/_info/);
    expect(calls).toHaveLength(1);
  });
});

test('fetches data', async () => {
  renderDashboardList(mockAdminUser);
  await waitFor(() => {
    const calls = fetchMock.callHistory.calls(/dashboard\/\?q/);
    expect(calls).toHaveLength(1);
  });

  const calls = fetchMock.callHistory.calls(/dashboard\/\?q/);
  expect(calls[0].url).toMatchInlineSnapshot(
    `"http://localhost/api/v1/dashboard/?q=(order_column:changed_on_delta_humanized,order_direction:desc,page:0,page_size:25,select_columns:!(id,dashboard_title,published,url,slug,description,changed_by,changed_by.id,changed_by.first_name,changed_by.last_name,changed_on_delta_humanized,editors.id,editors.label,editors.img,editors.type,tags.id,tags.name,tags.type,status,certified_by,certification_details,changed_on))"`,
  );
});

test('switches between card and table view', async () => {
  renderDashboardList(mockAdminUser);

  // Wait for the list to load
  await screen.findByTestId('dashboard-list-view');

  // Initially in card view (no table)
  expect(screen.queryByTestId('listview-table')).not.toBeInTheDocument();

  // Switch to table view via the list icon
  const listViewIcon = screen.getByRole('img', { name: 'unordered-list' });
  const listViewButton = listViewIcon.closest('button')!;
  fireEvent.click(listViewButton);

  await waitFor(() => {
    expect(screen.getByTestId('listview-table')).toBeInTheDocument();
  });

  // Switch back to card view
  const cardViewIcon = screen.getByRole('img', { name: 'appstore' });
  const cardViewButton = cardViewIcon.closest('button')!;
  fireEvent.click(cardViewButton);

  await waitFor(() => {
    expect(screen.queryByTestId('listview-table')).not.toBeInTheDocument();
  });
});

test('shows edit modal', async () => {
  renderDashboardList(mockAdminUser);

  // Wait for data to load
  await screen.findByText(mockDashboards[0].dashboard_title);

  // Find and click the first more options button
  const moreIcons = await screen.findAllByRole('img', {
    name: 'more',
  });
  fireEvent.click(moreIcons[0]);

  // Click edit from the dropdown
  const editButton = await screen.findByTestId(
    'dashboard-card-option-edit-button',
  );
  fireEvent.click(editButton);

  // Check for modal
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
});

test('shows delete confirmation', async () => {
  renderDashboardList(mockAdminUser);

  // Wait for data to load
  await screen.findByText(mockDashboards[0].dashboard_title);

  // Find and click the first more options button
  const moreIcons = await screen.findAllByRole('img', {
    name: 'more',
  });
  fireEvent.click(moreIcons[0]);

  // Click delete from the dropdown
  const deleteButton = await screen.findByTestId(
    'dashboard-card-option-delete-button',
  );
  fireEvent.click(deleteButton);

  // Check for confirmation dialog
  expect(
    await screen.findByText(/Are you sure you want to delete/i),
  ).toBeInTheDocument();
});

test('renders an "Import Dashboard" tooltip', async () => {
  renderDashboardList(mockAdminUser);

  const importButton = await screen.findByTestId('import-button');
  fireEvent.mouseOver(importButton);

  expect(
    await screen.findByRole('tooltip', {
      name: 'Import dashboards',
    }),
  ).toBeInTheDocument();
});

test('renders all standard filters', async () => {
  renderDashboardList(mockAdminUser);
  await screen.findByTestId('dashboard-list-view');

  // Verify filter labels exist
  expect(screen.getByText('Editor')).toBeInTheDocument();
  expect(screen.getByText('Status')).toBeInTheDocument();
  expect(screen.getByText('Modified by')).toBeInTheDocument();
  expect(screen.getByText('Certified')).toBeInTheDocument();
});

test('selecting Status filter encodes published=true in API call', async () => {
  renderDashboardList(mockAdminUser);
  await screen.findByTestId('dashboard-list-view');

  await waitFor(() => {
    expect(
      screen.getByText(mockDashboards[0].dashboard_title),
    ).toBeInTheDocument();
  });

  await selectPillOption('Published', 'Status');

  await waitFor(() => {
    const latest = getLatestDashboardApiCall();
    expect(latest).not.toBeNull();
    expect(latest!.query!.filters).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          col: 'published',
          opr: 'eq',
          value: true,
        }),
      ]),
    );
  });
});

test('selecting Editor filter encodes rel_m_m editors in API call', async () => {
  // Replace the editors route to return a selectable option
  fetchMock.removeRoutes({
    names: [API_ENDPOINTS.DASHBOARD_RELATED_EDITORS, API_ENDPOINTS.CATCH_ALL],
  });
  fetchMock.get(
    API_ENDPOINTS.DASHBOARD_RELATED_EDITORS,
    { result: [{ value: 1, text: 'Admin User' }], count: 1 },
    { name: API_ENDPOINTS.DASHBOARD_RELATED_EDITORS },
  );
  fetchMock.get(API_ENDPOINTS.CATCH_ALL, (callLog: any) => {
    const reqUrl =
      typeof callLog === 'string' ? callLog : callLog?.url || callLog;
    throw new Error(`[fetchMock catch-all] Unmatched GET: ${reqUrl}`);
  });

  renderDashboardList(mockAdminUser);
  await screen.findByTestId('dashboard-list-view');

  await waitFor(() => {
    expect(
      screen.getByText(mockDashboards[0].dashboard_title),
    ).toBeInTheDocument();
  });

  await selectPillOption('Admin User', 'Editor');

  await waitFor(() => {
    const latest = getLatestDashboardApiCall();
    expect(latest).not.toBeNull();
    expect(latest!.query!.filters).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          col: 'editors',
          opr: 'rel_m_m',
          value: 1,
        }),
      ]),
    );
  });
});

test('selecting Modified by filter encodes rel_o_m changed_by in API call', async () => {
  // Replace the changed_by route to return a selectable option
  fetchMock.removeRoutes({
    names: [
      API_ENDPOINTS.DASHBOARD_RELATED_CHANGED_BY,
      API_ENDPOINTS.CATCH_ALL,
    ],
  });
  fetchMock.get(
    API_ENDPOINTS.DASHBOARD_RELATED_CHANGED_BY,
    { result: [{ value: 1, text: 'Admin User' }], count: 1 },
    { name: API_ENDPOINTS.DASHBOARD_RELATED_CHANGED_BY },
  );
  fetchMock.get(API_ENDPOINTS.CATCH_ALL, (callLog: any) => {
    const reqUrl =
      typeof callLog === 'string' ? callLog : callLog?.url || callLog;
    throw new Error(`[fetchMock catch-all] Unmatched GET: ${reqUrl}`);
  });

  renderDashboardList(mockAdminUser);
  await screen.findByTestId('dashboard-list-view');

  await waitFor(() => {
    expect(
      screen.getByText(mockDashboards[0].dashboard_title),
    ).toBeInTheDocument();
  });

  await selectPillOption('Admin User', 'Modified by');

  await waitFor(() => {
    const latest = getLatestDashboardApiCall();
    expect(latest).not.toBeNull();
    expect(latest!.query!.filters).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          col: 'changed_by',
          opr: 'rel_o_m',
          value: 1,
        }),
      ]),
    );
  });
});

// One switch for object ownership (issue #69). The backend derives the
// OBJECT_OWNERSHIP flag from OWNERSHIP_ENABLED, so with the flag off the
// ownership blueprint is not loaded and every /api/v1/ownership/ call would
// 404. The page must therefore make none and show no ownership surface: no
// Owner or Sharing column, no row Sharing action, no drawer. Table view, where
// the row actions live (the card view has no ownership surface either way).
const ownershipListRoute = 'glob:*/api/v1/ownership/dashboards*';
// With the flag off the claim is about EVERY ownership route, not the list
// one: any call under /api/v1/ownership/ is counted against it.
const anyOwnershipRoute = 'glob:*/api/v1/ownership/*';

test('makes no ownership request and shows no ownership surface when OBJECT_OWNERSHIP is off', async () => {
  fetchMock.removeRoutes();
  fetchMock.get(anyOwnershipRoute, 404, {
    name: 'ownership-should-not-be-called',
  });
  setupMocks();
  mockIsFeatureEnabled.mockImplementation(() => false);

  renderDashboardList(mockAdminUser);
  await screen.findByText(mockDashboards[0].dashboard_title);
  // Rows and their actions are rendered before anything is asserted absent.
  await screen.findAllByTestId('dashboard-row-delete');
  const table = await screen.findByTestId('listview-table');
  const headers = within(table)
    .getAllByRole('columnheader')
    .map(header => header.textContent?.trim());
  expect(headers).not.toContain('Owner');
  expect(headers).not.toContain('Sharing');
  expect(screen.queryByTestId('dashboard-row-share')).not.toBeInTheDocument();
  expect(
    fetchMock.callHistory.calls('ownership-should-not-be-called'),
  ).toHaveLength(0);
});

test('fetches ownership once and shows the ownership surface when OBJECT_OWNERSHIP is on', async () => {
  fetchMock.removeRoutes();
  fetchMock.get(
    ownershipListRoute,
    {
      count: 1,
      result: [
        {
          object_id: mockDashboards[0].id,
          owner: { id: 1, name: 'Admin User', tenant_guid: null },
          visibility: 'private',
          unowned: false,
          can_manage: true,
          can_share: true,
        },
      ],
    },
    { name: 'ownership-list' },
  );
  setupMocks();
  mockIsFeatureEnabled.mockImplementation(
    (feature: string) => feature === 'OBJECT_OWNERSHIP',
  );

  renderDashboardList(mockAdminUser);
  await screen.findByText(mockDashboards[0].dashboard_title);
  await screen.findAllByTestId('dashboard-row-delete');
  const table = await screen.findByTestId('listview-table');
  const headers = within(table)
    .getAllByRole('columnheader')
    .map(header => header.textContent?.trim());
  expect(headers).toContain('Owner');
  expect(headers).toContain('Sharing');
  expect(screen.getAllByTestId('dashboard-row-share').length).toBeGreaterThan(
    0,
  );
  await waitFor(() => {
    expect(fetchMock.callHistory.calls('ownership-list')).toHaveLength(1);
  });
});
