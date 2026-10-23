"""Explicit candidate adapter fix; official CoMIK library remains unchanged."""

def retain_successful_source(source):
    changes=(
        ('  const Vector coarse_seed=output;',
         '  const Vector coarse_seed=output;\n  Vector last_success=output;'),
        ('    if(attempt(coarse_seed,coarse,"binary"))low=mid;',
         '    if(attempt(coarse_seed,coarse,"binary")) {low=mid;last_success=output;}'),
        ('  }\n  return true;\n}\n\nstatic std::vector<double> vector_field',
         '  }\n  output=last_success;\n  return true;\n}\n\nstatic std::vector<double> vector_field'))
    for old,new in changes:
        if source.count(old)!=1:
            raise ValueError('numeric refinement extension point changed; review required')
        source=source.replace(old,new)
    return source
