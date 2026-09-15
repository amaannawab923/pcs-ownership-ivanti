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
import { ErrorTypeEnum } from '@superset-ui/core';
import { getErrorMessageComponentRegistry } from 'src/components';
import { ChartSecurityAccessErrorMessage } from 'src/components/ErrorMessage/ChartSecurityAccessErrorMessage';

// Deployment-level error messages. Upstream ships this file as an empty
// function precisely so a deployment can register its own components here
// without editing setupErrorMessages.ts.
//
// PCS-10243 object ownership & sharing: a chart the viewer may not access
// (a private chart on a dashboard they can open, or its data request) is
// answered with CHART_SECURITY_ACCESS_ERROR; this component renders the
// placeholder tile with the chart's title and who to ask.
export default function setupErrorMessagesExtra() {
  getErrorMessageComponentRegistry().registerValue(
    ErrorTypeEnum.CHART_SECURITY_ACCESS_ERROR,
    ChartSecurityAccessErrorMessage,
  );
}
