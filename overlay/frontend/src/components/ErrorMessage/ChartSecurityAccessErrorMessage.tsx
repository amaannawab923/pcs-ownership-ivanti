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
import { ReactNode } from 'react';
import { t, tn } from '@apache-superset/core/translation';

import type { ErrorMessageComponentProps } from './types';
import { ErrorAlert } from './ErrorAlert';

interface ChartSecurityAccessExtra {
  owners?: string[];
  slice_name?: string;
  chart_id?: number;
}

/**
 * Shown when a viewer meets a chart they are not allowed to see
 * (CHART_SECURITY_ACCESS_ERROR) -- either as a dashboard tile whose chart was
 * never shared with them, or as the 403 from a chart-data request.
 *
 * Deliberately names the chart and its owners. The restricted thing is the
 * chart's DATA; its title is not, and withholding the title only makes the
 * denial unactionable -- a viewer who cannot name the chart cannot ask anyone
 * for access to it. Same reasoning, and same shape, as
 * `DatasourceSecurityAccessErrorMessage`.
 */
export function ChartSecurityAccessErrorMessage({
  error,
  closable,
  compact,
}: ErrorMessageComponentProps<ChartSecurityAccessExtra | null>) {
  const { extra, level, message } = error;
  const sliceName = extra?.slice_name;

  const explanation = sliceName
    ? t(
        'You do not have access to the chart "%s". Its data is hidden, but ' +
          'the chart is still part of this dashboard.',
        sliceName,
      )
    : t('You do not have access to this chart.');

  const owners = extra?.owners;
  const ownerLine =
    owners && owners.length > 0
      ? tn(
          'To request access, reach out to the chart owner: %s.',
          'To request access, reach out to the chart owners: %s.',
          owners.length,
          owners.join(', '),
        )
      : t('To request access, contact your Superset administrator.');

  const description: ReactNode = <div>{ownerLine}</div>;

  return (
    <ErrorAlert
      errorType={t("You don't have access to this chart")}
      message={explanation}
      description={description}
      descriptionDetails={message ? <pre>{message}</pre> : undefined}
      descriptionPre={false}
      type={level}
      closable={closable}
      compact={compact}
    />
  );
}
