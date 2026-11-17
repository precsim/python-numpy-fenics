% Public GNU Octave acceptance fixture for the packaged reference bridge.
% It discovers the package from this file, never from the current directory.

fprintf("reference_bridge_smoke_enter=pass\n");
fflush(stdout);
smoke_file = mfilename("fullpath");
if (isempty(smoke_file))
  error("reference_bridge:smoke", "cannot determine mock_smoke.m location");
endif
host_dir = fileparts(smoke_file);
package_root = fileparts(host_dir);
addpath(package_root, "-begin");
fprintf("reference_bridge_pre_bridge=pass\n");
fflush(stdout);

profile_path = fullfile(package_root, "profile.txt");
profile = strtrim(fileread(profile_path));
if (!any(strcmp(profile, {"core", "numpy", "fenics"})))
  error("reference_bridge:smoke", "unsupported profile marker: %s", profile);
endif

if (strcmp(profile, "fenics"))
  % Linux qualification intentionally makes the immutable FEniCS OpenBLAS
  % provider authoritative process-wide before Octave starts.  Exercise the
  % host's own LAPACK path before loading the bridge so an incompatible
  % process-global provider fails independently from the embedded solve.
  host_matrix = [4.0, 1.0; 1.0, 3.0];
  [host_vectors, host_values] = eig(host_matrix);
  host_residual = norm(host_matrix * host_vectors - host_vectors * host_values, "fro");
  assert(isfinite(host_residual));
  assert(host_residual <= 1.0e-10);
  fprintf("reference_bridge_octave_lapack=pass\n");
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
