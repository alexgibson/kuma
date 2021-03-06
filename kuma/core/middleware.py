import contextlib
import re
import urllib
from urlparse import urljoin

import brotli
import django.middleware.gzip
from django.conf import settings
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import urlresolvers
from django.http import (HttpResponseForbidden,
                         HttpResponsePermanentRedirect,
                         HttpResponseRedirect)
from django.utils import translation
from django.utils.cache import patch_vary_headers
from django.utils.encoding import iri_to_uri, smart_str
from whitenoise.middleware import WhiteNoiseMiddleware

from .urlresolvers import Prefixer, set_url_prefixer, split_path
from .utils import is_untrusted, urlparams
from .views import handler403


class LocaleURLMiddleware(object):
    """
    Based on zamboni.amo.middleware.
    Tried to use localeurl but it choked on 'en-US' with capital letters.

    1. Search for the locale.
    2. Save it in the request.
    3. Strip them from the URL.
    """

    def process_request(self, request):
        prefixer = Prefixer(request)
        set_url_prefixer(prefixer)
        full_path = prefixer.fix(prefixer.shortened_path)
        lang = request.GET.get('lang')

        if lang in dict(settings.LANGUAGES):
            # Blank out the locale so that we can set a new one. Remove lang
            # from the query params so we don't have an infinite loop.
            prefixer.locale = ''
            new_path = prefixer.fix(prefixer.shortened_path)
            query = dict((smart_str(k), v) for
                         k, v in request.GET.iteritems() if k != 'lang')

            # Never use HttpResponsePermanentRedirect here.
            # Its a temporary redirect and should return with http 302, not 301
            return HttpResponseRedirect(urlparams(new_path, **query))

        if full_path != request.path:
            query_string = request.META.get('QUERY_STRING', '')
            full_path = urllib.quote(full_path.encode('utf-8'))

            if query_string:
                full_path = '%s?%s' % (full_path, query_string)

            response = HttpResponseRedirect(full_path)

            # Vary on Accept-Language if we changed the locale
            old_locale = prefixer.locale
            new_locale, _ = split_path(full_path)
            if old_locale != new_locale:
                response['Vary'] = 'Accept-Language'

            return response

        request.path_info = '/' + prefixer.shortened_path
        request.LANGUAGE_CODE = prefixer.locale or settings.LANGUAGE_CODE
        # prefixer.locale can be '', but we need a real locale code to activate
        # otherwise the request uses the previously handled request's
        # translations.
        translation.activate(prefixer.locale or settings.LANGUAGE_CODE)

    def process_response(self, request, response):
        """Unset the thread-local var we set during `process_request`."""
        # This makes mistaken tests (that should use LocalizingClient but
        # use Client instead) fail loudly and reliably. Otherwise, the set
        # prefixer bleeds from one test to the next, making tests
        # order-dependent and causing hard-to-track failures.
        set_url_prefixer(None)
        return response

    def process_exception(self, request, exception):
        set_url_prefixer(None)


class Forbidden403Middleware(object):
    """
    Renders a 403.html page if response.status_code == 403.
    """

    def process_response(self, request, response):
        if isinstance(response, HttpResponseForbidden):
            return handler403(request)
        # If not 403, return response unmodified
        return response


def is_valid_path(request, path):
    urlconf = getattr(request, 'urlconf', None)
    try:
        urlresolvers.resolve(path, urlconf)
        return True
    except urlresolvers.Resolver404:
        return False


class RemoveSlashMiddleware(object):
    """
    Middleware that tries to remove a trailing slash if there was a 404.

    If the response is a 404 because url resolution failed, we'll look for a
    better url without a trailing slash.
    """

    def process_response(self, request, response):
        if (response.status_code == 404 and
                request.path_info.endswith('/') and
                not is_valid_path(request, request.path_info) and
                is_valid_path(request, request.path_info[:-1])):
            # Use request.path because we munged app/locale in path_info.
            newurl = request.path[:-1]
            if request.GET:
                with safe_query_string(request):
                    newurl += '?' + request.META['QUERY_STRING']
            return HttpResponsePermanentRedirect(newurl)
        return response


@contextlib.contextmanager
def safe_query_string(request):
    """
    Turn the QUERY_STRING into a unicode- and ascii-safe string.

    We need unicode so it can be combined with a reversed URL, but it has to be
    ascii to go in a Location header.  iri_to_uri seems like a good compromise.
    """
    qs = request.META['QUERY_STRING']
    try:
        request.META['QUERY_STRING'] = iri_to_uri(qs)
        yield
    finally:
        request.META['QUERY_STRING'] = qs


class SetRemoteAddrFromForwardedFor(object):
    """
    Middleware that sets REMOTE_ADDR based on HTTP_X_FORWARDED_FOR, if the
    latter is set. This is useful if you're sitting behind a reverse proxy that
    causes each request's REMOTE_ADDR to be set to 127.0.0.1.
    """

    def process_request(self, request):
        try:
            forwarded_for = request.META['HTTP_X_FORWARDED_FOR']
        except KeyError:
            pass
        else:
            # HTTP_X_FORWARDED_FOR can be a comma-separated list of IPs.
            # The client's IP will be the first one.
            forwarded_for = forwarded_for.split(',')[0].strip()
            request.META['REMOTE_ADDR'] = forwarded_for


class ForceAnonymousSessionMiddleware(SessionMiddleware):

    def process_request(self, request):
        """
        Always create an anonymous session.
        """
        request.session = self.SessionStore(None)

    def process_response(self, request, response):
        """
        Override the base-class method to ensure we do nothing.
        """
        return response


class RestrictedEndpointsMiddleware(object):

    def process_request(self, request):
        """
        Restricts the accessible endpoints based on the host.
        """
        if settings.ENABLE_RESTRICTIONS_BY_HOST and is_untrusted(request):
            request.urlconf = 'kuma.urls_untrusted'


class RestrictedWhiteNoiseMiddleware(WhiteNoiseMiddleware):

    def process_request(self, request):
        """
        Restricts the use of WhiteNoiseMiddleware based on the host.
        """
        if settings.ENABLE_RESTRICTIONS_BY_HOST and is_untrusted(request):
            return None
        return super(RestrictedWhiteNoiseMiddleware, self).process_request(
            request
        )


class LegacyDomainRedirectsMiddleware(object):

    def process_request(self, request):
        """
        Permanently redirects all requests from legacy domains.
        """
        if request.get_host() in settings.LEGACY_HOSTS:
            return HttpResponsePermanentRedirect(
                urljoin(settings.SITE_URL, request.get_full_path())
            )
        return None


class GZipMiddleware(django.middleware.gzip.GZipMiddleware):
    """
    This is identical to Django's GZipMiddleware, except that it will not
    modify the ETag header.

    TODO: When moving to Django 1.11, this code and its tests can be deleted,
          and django.middleware.gzip.GZipMiddleware should be used instead.
    """

    def process_response(self, request, response):
        original_etag = response.get('etag')
        response_out = super(GZipMiddleware, self).process_response(
            request,
            response
        )
        if (original_etag is not None) and response_out.has_header('etag'):
            response_out['etag'] = original_etag
        return response_out


class BrotliMiddleware(object):
    """
    This middleware enables Brotli compression

    This code is inspired by https://github.com/illagrenan/django-brotli
    """

    MIN_LEN_RESPONSE_TO_PROCESS = 200
    RE_ACCEPT_ENCODING_BROTLI = re.compile(r'\bbr\b')

    def _accepts_brotli_encoding(self, request):
        return bool(self.RE_ACCEPT_ENCODING_BROTLI.search(
            request.META.get('HTTP_ACCEPT_ENCODING', '')))

    def process_response(self, request, response):
        if (response.streaming or
                response.has_header('Content-Encoding') or
                not self._accepts_brotli_encoding(request) or
                len(response.content) < self.MIN_LEN_RESPONSE_TO_PROCESS):
            # ---------
            # 1) Skip streaming content, GZipMiddleware will compress it
            #    (supported, see https://github.com/google/brotli/issues/191).
            # 2) Skip if the content is already encoded.
            # 3) Skip if client didn't request brotli.
            # 4) Skip if the content is short, compressing isn't worth it
            #    (same logic as Django's GZipMiddleware).
            # ---------
            return response

        compressed_content = brotli.compress(response.content, quality=5)

        # Return the uncompressed content if compression didn't help
        if len(compressed_content) >= len(response.content):
            return response

        response.content = compressed_content
        patch_vary_headers(response, ('Accept-Encoding',))
        response['Content-Length'] = str(len(compressed_content))
        response['Content-Encoding'] = 'br'
        return response
