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
import { Tag } from '@superset-ui/core/components';
import type { OwnershipVisibility } from './types';

const LABELS: Record<OwnershipVisibility, string> = {
  private: t('Private'),
  shared: t('Shared'),
  public: t('Public'),
};

const COLORS: Record<OwnershipVisibility, string> = {
  private: 'default',
  shared: 'blue',
  public: 'green',
};

export default function VisibilityTag({
  visibility,
  disabled,
}: {
  visibility?: OwnershipVisibility | null;
  disabled?: boolean;
}) {
  if (disabled) {
    return <Tag color="warning">{t('Disabled')}</Tag>;
  }
  if (!visibility || !LABELS[visibility]) {
    return <Tag>{t('Unknown')}</Tag>;
  }
  return <Tag color={COLORS[visibility]}>{LABELS[visibility]}</Tag>;
}
