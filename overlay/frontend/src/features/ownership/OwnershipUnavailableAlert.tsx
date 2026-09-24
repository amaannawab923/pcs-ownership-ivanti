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
import { styled, css } from '@apache-superset/core/theme';
import { Button } from '@superset-ui/core/components';

// Issue #122: when the ownership list cannot be read, every row's Owner and
// Sharing read "Unknown" -- which is exactly what a genuinely unowned object
// reads as. Not inventing an owner is right; leaving the page silent about
// it is not, because a hundred rows that look unowned are indistinguishable
// from the truth. This says which one it is, once, above the list.
//
// Deliberately not a toast: the state persists for as long as the store is
// unreachable and the user keeps looking at the affected columns, so it
// belongs on the page rather than in something that fades.
const Alert = styled.div`
  ${({ theme }) => css`
    display: flex;
    align-items: center;
    gap: ${theme.sizeUnit * 2}px;
    margin: 0 ${theme.sizeUnit * 4}px ${theme.sizeUnit * 2}px;
    padding: ${theme.sizeUnit * 2}px ${theme.sizeUnit * 3}px;
    background: ${theme.colorWarningBg};
    border: 1px solid ${theme.colorWarningBorder};
    border-radius: ${theme.borderRadius}px;
    color: ${theme.colorText};
    font-size: ${theme.fontSizeSM}px;
    line-height: 1.5;
  `}
`;

export interface OwnershipUnavailableAlertProps {
  // Whether the last attempt to read the ownership list failed.
  unavailable: boolean;
  // Try again, without reloading the page.
  onRetry: () => void;
}

export default function OwnershipUnavailableAlert({
  unavailable,
  onRetry,
}: OwnershipUnavailableAlertProps) {
  if (!unavailable) return null;
  return (
    <Alert role="status" data-test="ownership-unavailable">
      <span>
        {t(
          'Ownership information could not be loaded, so Owner and Sharing show as Unknown for every row. This does not mean these objects have no owner.',
        )}
      </span>
      <Button buttonSize="small" buttonStyle="link" onClick={onRetry}>
        {t('Try again')}
      </Button>
    </Alert>
  );
}
