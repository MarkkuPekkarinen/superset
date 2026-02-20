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
// ***********************************************
// Tests for links in the explore UI
// ***********************************************

import rison from 'rison';
import { nanoid } from 'nanoid';
import { interceptChart } from 'cypress/utils';
import { HEALTH_POP_FORM_DATA_DEFAULTS } from './visualizations/shared.helper';

const apiURL = (endpoint: string, queryObject: Record<string, unknown>) =>
  `${endpoint}?q=${rison.encode(queryObject)}`;

describe('Test explore links', () => {
  beforeEach(() => {
    interceptChart({ legacy: false }).as('chartData');
  });

  it('Open and close view query modal', () => {
    cy.visitChartByName('Growth Rate');
    cy.verifySliceSuccess({ waitAlias: '@chartData' });

    cy.get('[aria-label="Menu actions trigger"]').click();
    cy.get('span').contains('View query').parent().click();
    cy.wait('@chartData').then(() => {
      cy.get('code');
    });
    cy.get('.ant-modal-content').within(() => {
      cy.get('button.ant-modal-close').first().click({ force: true });
    });
  });

  it('Test iframe link', () => {
    cy.visitChartByName('Growth Rate');
    cy.verifySliceSuccess({ waitAlias: '@chartData' });

    cy.get('[aria-label="Menu actions trigger"]').click();
    cy.get('div[role="menuitem"]').within(() => {
      cy.contains('Share').parent().click();
    });
    cy.getBySel('embed-code-button').click();
    cy.get('#embed-code-popover').within(() => {
      cy.get('textarea[name=embedCode]').contains('iframe');
    });
  });

  it('Test chart save as AND overwrite', () => {
    interceptChart({ legacy: false }).as('tableChartData');

    const formData = {
      ...HEALTH_POP_FORM_DATA_DEFAULTS,
      viz_type: 'table',
      metrics: ['sum__SP_POP_TOTL'],
      groupby: ['country_name'],
    };
    const newChartName = `Test chart [${nanoid()}]`;

    cy.visitChartByParams(formData);
    cy.verifySliceSuccess({ waitAlias: '@tableChartData' });
    cy.url().then(() => {
      cy.get('[data-test="query-save-button"]').click();
      cy.get('[data-test="saveas-radio"]').check();
      cy.get('[data-test="new-chart-name"]').type(newChartName, {
        force: true,
      });
      cy.get('[data-test="btn-modal-save"]').click();
      cy.verifySliceSuccess({ waitAlias: '@tableChartData' });
      cy.visitChartByName(newChartName);

      // Overwriting!
      cy.get('[data-test="query-save-button"]').click();
      cy.get('[data-test="save-overwrite-radio"]').check();
      cy.get('[data-test="btn-modal-save"]').click();
      cy.verifySliceSuccess({ waitAlias: '@tableChartData' });
      const query = {
        filters: [
          {
            col: 'slice_name',
            opr: 'eq',
            value: newChartName,
          },
        ],
      };

      cy.request(apiURL('/api/v1/chart/', query)).then(response => {
        expect(response.body.count).to.be.at.least(1);
      });
      cy.deleteChartByName(newChartName, true);
    });
  });

  it('Test chart save as and add to new dashboard', () => {
    const chartName = 'Growth Rate';
    const newChartName = `${chartName} [${nanoid()}]`;
    const dashboardTitle = `Test dashboard [${nanoid()}]`;
    const saveDashboardFormSelector =
      '[data-test="save-chart-modal-select-dashboard-form"]';

    const selectDashboard = (title: string) => {
      cy.get(saveDashboardFormSelector)
        .find('input[aria-label^="Select a dashboard"]')
        .click({ force: true })
        .clear({ force: true })
        .type(title, { force: true });

      cy.get('body').then($body => {
        const selector = '.ant-select-item-option-content';
        const option = $body
          .find(selector)
          .filter((_, el) => el.textContent === title);

        if (option.length > 0) {
          cy.wrap(option[0]).click({ force: true });
        } else {
          cy.get(saveDashboardFormSelector)
            .find('input[aria-label^="Select a dashboard"]')
            .type('{enter}', { force: true });
        }
      });

      cy.get('[data-test="btn-modal-save"]').should('not.be.disabled');
    };

    cy.visitChartByName(chartName);
    cy.verifySliceSuccess({ waitAlias: '@chartData' });

    cy.get('[data-test="query-save-button"]').click();
    cy.get('[data-test="saveas-radio"]').check();
    cy.get('[data-test="new-chart-name"]').click();
    cy.get('[data-test="new-chart-name"]').clear();
    cy.get('[data-test="new-chart-name"]').type(newChartName);
    // Add a new option using the "CreatableSelect" feature
    selectDashboard(dashboardTitle);

    cy.get('[data-test="btn-modal-save"]').click();
    cy.verifySliceSuccess({ waitAlias: '@chartData' });
    cy.contains(`was added to dashboard [${dashboardTitle}]`);

    cy.visitChartByName(newChartName);
    cy.verifySliceSuccess({ waitAlias: '@chartData' });

    cy.get('[data-test="query-save-button"]').click();
    cy.get('[data-test="save-overwrite-radio"]').check();
    cy.get('[data-test="new-chart-name"]').click();
    cy.get('[data-test="new-chart-name"]').clear();
    cy.get('[data-test="new-chart-name"]').type(newChartName);
    // This time around, typing the same dashboard name
    // will select the existing one
    selectDashboard(dashboardTitle);

    cy.get('[data-test="btn-modal-save"]').click();
    cy.verifySliceSuccess({ waitAlias: '@chartData' });
    let query = {
      filters: [
        {
          col: 'slice_name',
          opr: 'eq',
          value: chartName,
        },
      ],
    };
    cy.request(apiURL('/api/v1/chart/', query)).then(response => {
      expect(response.body.count).to.be.at.least(1);
    });
    cy.deleteDashboardByName(dashboardTitle, true);
  });
});
