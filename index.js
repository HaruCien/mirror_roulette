export default {
  async fetch(request, env) {
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*', // 필요시 실제 GitHub Pages 도메인으로 좁혀도 됨
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: corsHeaders });
    }

    const url = new URL(request.url);

    // 관리자 비밀번호 확인 (로그인)
    if (url.pathname === '/login' && request.method === 'POST') {
      try {
        const body = await request.json();
        const ok = body.password === env.ADMIN_PASSWORD;
        return jsonResponse({ ok }, ok ? 200 : 401, corsHeaders);
      } catch (e) {
        return jsonResponse({ ok: false, error: 'bad request' }, 400, corsHeaders);
      }
    }

    // 인격 목록 조회 - 누구나 가능 (읽기 전용)
    if (url.pathname === '/identities' && request.method === 'GET') {
      const stored = await env.IDENTITIES_KV.get('identities');
      return jsonResponse(
        { ok: true, data: stored ? JSON.parse(stored) : null },
        200,
        corsHeaders
      );
    }

    // 인격 목록 저장 - 관리자 비밀번호가 맞을 때만
    if (url.pathname === '/identities' && request.method === 'POST') {
      try {
        const body = await request.json();
        if (body.password !== env.ADMIN_PASSWORD) {
          return jsonResponse({ ok: false, error: 'unauthorized' }, 401, corsHeaders);
        }
        await env.IDENTITIES_KV.put('identities', JSON.stringify(body.data));
        return jsonResponse({ ok: true }, 200, corsHeaders);
      } catch (e) {
        return jsonResponse({ ok: false, error: 'bad request' }, 400, corsHeaders);
      }
    }

    return new Response('Not Found', { status: 404, headers: corsHeaders });
  },
};

function jsonResponse(obj, status, corsHeaders) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...corsHeaders, 'Content-Type': 'application/json' },
  });
}