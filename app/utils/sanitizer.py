import bleach

def sanitize_input(data):
  
    if isinstance(data, str):
        return bleach.clean(data, tags=[], strip=True)
    
    if isinstance(data, dict):
        return {k: sanitize_input(v) for k, v in data.items()}
    
    if isinstance(data, list):
        return [sanitize_input(i) for i in data]
    
    return data