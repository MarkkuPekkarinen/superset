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
import { ReactChild, useCallback, Key } from 'react';

import { t, styled } from '@superset-ui/core';
import { Icons } from '@superset-ui/core/components/Icons';
import { Dropdown } from '@superset-ui/core/components';

enum MenuKeys {
  ExportOriginal = 'export_original',
  ExportPivoted = 'export_pivoted',
}

interface ExportToCSVButtonProps {
  exportCSVOriginal: () => void;
  exportCSVPivoted: () => void;
  children: ReactChild;
}

const MenuItemContent = styled.div`
  display: flex;
  align-items: center;
  justify-content: space-between;

  span[role='img'] {
    font-size: ${({ theme }) => theme.fontSizeLG}px;
    margin-left: ${({ theme }) => theme.sizeUnit * 4}px;
  }
`;

export const ExportToCSVDropdown = ({
  exportCSVOriginal,
  exportCSVPivoted,
  children,
}: ExportToCSVButtonProps) => {
  const handleMenuClick = useCallback(
    ({ key }: { key: Key }) => {
      switch (key) {
        case MenuKeys.ExportOriginal:
          exportCSVOriginal();
          break;
        case MenuKeys.ExportPivoted:
          exportCSVPivoted();
          break;
        default:
          break;
      }
    },
    [exportCSVPivoted, exportCSVOriginal],
  );

  return (
    <Dropdown
      trigger={['click']}
      menu={{
        onClick: handleMenuClick,
        selectable: false,
        items: [
          {
            key: MenuKeys.ExportOriginal,
            label: (
              <MenuItemContent>
                {t('Original')}
                <Icons.DownloadOutlined />
              </MenuItemContent>
            ),
          },
          {
            key: MenuKeys.ExportPivoted,
            label: (
              <MenuItemContent>
                {t('Pivoted')}
                <Icons.DownloadOutlined />
              </MenuItemContent>
            ),
          },
        ],
      }}
    >
      {children}
    </Dropdown>
  );
};
