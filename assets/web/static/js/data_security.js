// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 fiyo (Jack Ge) <sdfiyon@gmail.com>
// Author: fiyo (Jack Ge) - https://github.com/fiyo/DBCheck
(() => {

// SPDX-License-Identifier: Apache-2.0
// Copyright 2025-2026 sec.hn.cn (X.U zx) <xuzhongxin@sec.hn.cn>
// Author: sec.hn.cn  (X.U zx) - https://github.com/zhongxinxuhk/DBCheck

'use strict';
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const method = (options.method || (input instanceof Request ? input.method : 'GET')).toUpperCase();
    if (url.origin === location.origin && !['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      const headers = new Headers(options.headers || (input instanceof Request ? input.headers : undefined));
      const token = document.querySelector('meta[name="csrf-token"]')?.content;
      if (token) headers.set('X-CSRF-Token', token);
      options = {...options, headers};
    }
    return originalFetch(input, options);
  };
})();
