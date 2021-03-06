#
# Copyright © 2012 - 2020 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.utils.encoding import force_str
from django.utils.translation import gettext as _
from django.utils.translation import ngettext
from django.views.decorators.http import require_POST

from weblate.lang.models import Language
from weblate.trans.exceptions import PluralFormsMismatch
from weblate.trans.forms import DownloadForm, get_upload_form
from weblate.trans.models import ComponentList, Translation
from weblate.utils import messages
from weblate.utils.data import data_dir
from weblate.utils.errors import report_error
from weblate.utils.views import (
    download_translation_file,
    get_component,
    get_project,
    get_translation,
    show_form_errors,
    zip_download,
)


def download_multi(translations, fmt=None):
    filenames = [t.get_filename() for t in translations]
    return zip_download(
        data_dir("vcs"), [filename for filename in filenames if filename]
    )


def download_component_list(request, name):
    obj = get_object_or_404(ComponentList, slug__iexact=name)
    components = obj.components.filter(project_id__in=request.user.allowed_project_ids)
    for component in components:
        component.commit_pending("download", None)
    return download_multi(
        Translation.objects.filter(component__in=components), request.GET.get("format")
    )


def download_component(request, project, component):
    obj = get_component(request, project, component)
    obj.commit_pending("download", None)
    return download_multi(obj.translation_set.all(), request.GET.get("format"))


def download_project(request, project):
    obj = get_project(request, project)
    obj.commit_pending("download", None)
    return download_multi(
        Translation.objects.filter(component__project=obj), request.GET.get("format")
    )


def download_lang_project(request, lang, project):
    obj = get_project(request, project)
    obj.commit_pending("download", None)
    langobj = get_object_or_404(Language, code=lang)
    return download_multi(
        Translation.objects.filter(component__project=obj, language=langobj),
        request.GET.get("format"),
    )


def download_translation(request, project, component, lang):
    obj = get_translation(request, project, component, lang)

    kwargs = {}

    if "format" in request.GET or "q" in request.GET:
        form = DownloadForm(request.GET)
        if not form.is_valid():
            show_form_errors(request, form)
            return redirect(obj)

        kwargs["units"] = obj.unit_set.search(form.cleaned_data.get("q", "")).distinct()
        kwargs["fmt"] = form.cleaned_data["format"]

    return download_translation_file(obj, **kwargs)


@require_POST
def upload_translation(request, project, component, lang):
    """Handling of translation uploads."""
    obj = get_translation(request, project, component, lang)

    if not request.user.has_perm("upload.perform", obj):
        raise PermissionDenied()

    # Check method and lock
    if obj.component.locked:
        messages.error(request, _("Access denied."))
        return redirect(obj)

    # Get correct form handler based on permissions
    form = get_upload_form(request.user, obj, request.POST, request.FILES)

    # Check form validity
    if not form.is_valid():
        messages.error(request, _("Please fix errors in the form."))
        show_form_errors(request, form)
        return redirect(obj)

    # Create author name
    author_name = None
    author_email = None
    if request.user.has_perm("upload.authorship", obj):
        author_name = form.cleaned_data["author_name"]
        author_email = form.cleaned_data["author_email"]

    # Check for overwriting
    overwrite = False
    if request.user.has_perm("upload.overwrite", obj):
        overwrite = form.cleaned_data["upload_overwrite"]

    # Do actual import
    try:
        not_found, skipped, accepted, total = obj.merge_upload(
            request,
            request.FILES["file"],
            overwrite,
            author_name,
            author_email,
            method=form.cleaned_data["method"],
            fuzzy=form.cleaned_data["fuzzy"],
        )
        if total == 0:
            message = _("No strings were imported from the uploaded file.")
        else:
            message = ngettext(
                "Processed {0} string from the uploaded files "
                "(skipped: {1}, not found: {2}, updated: {3}).",
                "Processed {0} strings from the uploaded files "
                "(skipped: {1}, not found: {2}, updated: {3}).",
                total,
            ).format(total, skipped, not_found, accepted)
        if accepted == 0:
            messages.warning(request, message)
        else:
            messages.success(request, message)
    except PluralFormsMismatch:
        messages.error(
            request,
            _("Plural forms in the uploaded file do not match current translation."),
        )
    except Exception as error:
        messages.error(
            request,
            _("File upload has failed: %s")
            % force_str(error).replace(obj.component.full_path, ""),
        )
        report_error(cause="Upload error")

    return redirect(obj)
