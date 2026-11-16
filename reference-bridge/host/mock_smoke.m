% Public GNU Octave acceptance fixture for the packaged reference bridge.
% It discovers the package from this file, never from the current directory.

smoke_file = mfilename("fullpath");
if (isempty(smoke_file))
  error("reference_bridge:smoke", "cannot determine mock_smoke.m location");
endif
host_dir = fileparts(smoke_file);
package_root = fileparts(host_dir);
addpath(package_root, "-begin");

profile_path = fullfile(package_root, "profile.txt");
profile = strtrim(fileread(profile_path));
if (!any(strcmp(profile, {"core", "numpy", "fenics"})))
  error("reference_bridge:smoke", "unsupported profile marker: %s", profile);
endif

info = bridge("runtime");
assert(isstruct(info));
assert(strcmp(info.python_version, "3.14.10"));
assert(strcmp(info.python_abi, "cp314"));
assert(strcmp(info.profile, profile));
assert(!isempty(info.runtime_root));

assert(bridge("scalar", 5.0) == 11.0);

if (strcmp(getenv("REFERENCE_BRIDGE_FRESH_ONLY"), "1"))
  % A fresh process must exercise the selected native capability, not just
  % CPython startup: NumPy/FFCx/PETSc loader faults may occur only on import.
  if (strcmp(profile, "numpy"))
    assert(bridge("numpy", 5.0) == 55.0);
  elseif (strcmp(profile, "fenics"))
    assert(bridge("numpy", 5.0) == 55.0);
    assert(bridge("fenics") <= 5.0e-2);
  endif
  fprintf("reference_bridge_result=pass\n");
  return;
endif

% A handled host-side validation failure must not poison embedded Python.
did_fail = false;
try
  ignored = bridge("scalar", [1.0, 2.0]); %#ok<NASGU>
catch
  did_fail = true;
end_try_catch
assert(did_fail);
assert(bridge("scalar", 10.0) == 21.0);
assert(bridge("scalar", 5.0) == 11.0);

% Force a backend-side numerical failure and prove Python exception recovery.
did_fail = false;
try
  ignored = bridge("scalar", realmax); %#ok<NASGU>
catch err
  did_fail = strcmp(err.identifier, "reference_bridge:python") || ...
             !isempty(strfind(err.message, "non-finite"));
end_try_catch
assert(did_fail);
assert(bridge("scalar", 5.0) == 11.0);

did_fail = false;
try
  ignored = bridge("unknown"); %#ok<NASGU>
catch
  did_fail = true;
end_try_catch
assert(did_fail);

if (strcmp(profile, "core"))
  did_fail = false;
  try
    ignored = bridge("numpy", 5.0); %#ok<NASGU>
  catch err
    did_fail = strcmp(err.identifier, "reference_bridge:profile") || ...
               !isempty(strfind(err.message, "unavailable"));
  end_try_catch
  assert(did_fail);

  did_fail = false;
  try
    ignored = bridge("fenics"); %#ok<NASGU>
  catch err
    did_fail = strcmp(err.identifier, "reference_bridge:profile") || ...
               !isempty(strfind(err.message, "unavailable"));
  end_try_catch
  assert(did_fail);
elseif (strcmp(profile, "numpy"))
  assert(bridge("numpy", 5.0) == 55.0);
  assert(bridge("numpy", 5.0) == 55.0);

  did_fail = false;
  try
    ignored = bridge("fenics"); %#ok<NASGU>
  catch err
    did_fail = strcmp(err.identifier, "reference_bridge:profile") || ...
               !isempty(strfind(err.message, "unavailable"));
  end_try_catch
  assert(did_fail);
else
  assert(bridge("numpy", 5.0) == 55.0);
  fenics_error = bridge("fenics");
  assert(isfinite(fenics_error));
  assert(fenics_error <= 5.0e-2);
endif

fprintf("reference_bridge_result=pass\n");
