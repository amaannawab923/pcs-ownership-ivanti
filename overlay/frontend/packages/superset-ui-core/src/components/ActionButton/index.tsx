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

import type { ReactElement, ReactNode } from 'react';
import cx from 'classnames';
import { Tooltip, type TooltipPlacement } from '@superset-ui/core/components';
import { css, useTheme } from '@apache-superset/core/theme';

export interface ActionProps {
  label: string;
  tooltip?: string | ReactElement;
  placement?: TooltipPlacement;
  icon: ReactNode;
  onClick: () => void;
  className?: string;
  disabled?: boolean;
  dataTest?: string;
}

export const ActionButton = ({
  label,
  tooltip,
  placement,
  icon,
  onClick,
  className,
  disabled = false,
  dataTest,
}: ActionProps) => {
  const theme = useTheme();
  const actionButton = (
    <button
      type="button"
      // Disabling via CSS/aria alone still leaves the control operable by
      // anything that doesn't go through onClick (e.g. a stray keydown
      // handler, or a test/automation tool inspecting the DOM), and reads
      // as fully enabled to accessibility tooling. Set the real HTML
      // `disabled` attribute so the button is genuinely inert, in addition
      // to the aria/class hooks kept below for existing styling.
      disabled={disabled}
      aria-disabled={disabled}
      aria-label={typeof tooltip === 'string' ? tooltip : label}
      css={css`
        appearance: none;
        border: none;
        background: none;
        padding: 0;
        margin: 0;
        font: inherit;
        line-height: 1;
        display: inline-flex;
        align-items: center;
        cursor: pointer;
        color: ${theme.colorIcon};
        margin-right: ${theme.sizeUnit}px;
        &:not(.disabled):hover {
          path {
            fill: ${theme.colorPrimary};
          }
        }
        &.disabled {
          color: ${theme.colorTextDisabled};
          cursor: not-allowed;
        }
      `}
      className={cx('action-button', className, { disabled })}
      data-test={dataTest ?? label}
      onClick={disabled ? undefined : onClick}
    >
      {icon}
    </button>
  );

  const tooltipId = `${label.replaceAll(' ', '-').toLowerCase()}-tooltip`;

  if (!tooltip) {
    return actionButton;
  }

  // A native `disabled` button doesn't dispatch mouse/hover events in most
  // browsers, which would silently swallow the tooltip right when it's
  // most needed (explaining *why* the control is disabled). Give the
  // tooltip a non-disabled span to attach its hover listener to instead.
  return (
    <Tooltip id={tooltipId} title={tooltip} placement={placement}>
      {disabled ? (
        <span
          css={css`
            display: inline-flex;
          `}
        >
          {actionButton}
        </span>
      ) : (
        actionButton
      )}
    </Tooltip>
  );
};
