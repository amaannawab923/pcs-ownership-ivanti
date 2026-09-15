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

import { t } from '@apache-superset/core/translation';
import { css, useTheme } from '@apache-superset/core/theme';
import { Tooltip } from '@superset-ui/core/components';
import { Icons } from '@superset-ui/core/components/Icons';
import type { OwnershipListItem } from './types';

export interface OwnerCellProps {
  ownership: OwnershipListItem;
}

// Renders the Owner column cell for the object-ownership list views. When
// the recorded owner's account has been deactivated (`unowned`), the object
// keeps its visibility but nobody may change its sharing until an admin
// reassigns ownership -- surface that instead of showing what looks like an
// ordinary, actionable owner.
export default function OwnerCell({ ownership }: OwnerCellProps) {
  const theme = useTheme();
  const name = ownership.owner?.name ?? t('Unknown');

  if (!ownership.unowned) {
    return <span>{name}</span>;
  }

  return (
    <span
      css={css`
        display: inline-flex;
        align-items: center;
        gap: ${theme.sizeUnit}px;
      `}
    >
      {name}
      <Tooltip
        title={t(
          'This owner’s account has been deactivated. An administrator must assign a new owner before this object can be shared.',
        )}
      >
        <Icons.WarningOutlined
          iconSize="s"
          iconColor={theme.colorWarning}
          data-test="unowned-indicator"
        />
      </Tooltip>
    </span>
  );
}
