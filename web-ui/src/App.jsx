import { useState, useCallback, useRef } from 'react'
import './App.css'

function App() {
  const [dragActive, setDragActive] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [results, setResults] = useState([])
  const [selectedType, setSelectedType] = useState('auto')
  const fileInputRef = useRef(null)

  const handleDrag = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true)
    } else if (e.type === 'dragleave') {
      setDragActive(false)
    }
  }, [])

  const handleDrop = useCallback((e) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)
    
    const files = Array.from(e.dataTransfer.files)
    if (files.length > 0) {
      uploadFiles(files)
    }
  }, [])

  const handleFileSelect = (e) => {
    const files = Array.from(e.target.files)
    if (files.length > 0) {
      uploadFiles(files)
    }
  }

  const uploadFiles = async (files) => {
    setUploading(true)
    const newResults = []
    
    for (const file of files) {
      const formData = new FormData()
      formData.append('file', file)
      
      try {
        const response = await fetch('http://127.0.0.1:8000/ingest', {
          method: 'POST',
          body: formData,
        })
        
        if (response.ok) {
          const data = await response.json()
          newResults.push({
            file: file.name,
            type: file.type || 'unknown',
            status: 'success',
            ...data
          })
        } else {
          const error = await response.json()
          newResults.push({
            file: file.name,
            type: file.type || 'unknown',
            status: 'error',
            error: error.detail || 'Upload failed'
          })
        }
      } catch (err) {
        newResults.push({
          file: file.name,
          type: file.type || 'unknown',
          status: 'error',
          error: err.message
        })
      }
    }
    
    setResults(prev => [...prev, ...newResults])
    setUploading(false)
  }

  const handleUrlIngest = async (e) => {
    e.preventDefault()
    const formData = new FormData(e.target)
    const url = formData.get('url')
    
    if (!url) return
    
    setUploading(true)
    
    try {
      const response = await fetch('http://127.0.0.1:8000/ingest', {
        method: 'POST',
        body: new URLSearchParams({ url }),
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
        },
      })
      
      if (response.ok) {
        const data = await response.json()
        setResults(prev => [{
          file: url,
          type: 'url',
          status: 'success',
          ...data
        }, ...prev])
      } else {
        const error = await response.json()
        setResults(prev => [{
          file: url,
          type: 'url',
          status: 'error',
          error: error.detail || 'Ingestion failed'
        }, ...prev])
      }
    } catch (err) {
      setResults(prev => [{
        file: url,
        type: 'url',
        status: 'error',
        error: err.message
      }, ...prev])
    }
    
    setUploading(false)
    e.target.reset()
  }

  const formatFileSize = (bytes) => {
    if (bytes < 1024) return `${bytes} B`
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  }

  const getStatusColor = (status) => {
    switch (status) {
      case 'success': return '#10b981'
      case 'error': return '#ef4444'
      case 'pending': return '#f59e0b'
      default: return '#6b7280'
    }
  }

  return (
    <div className="app">
      <header className="header">
        <div className="logo">
          <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
            <polyline points="14 2 14 8 20 8"></polyline>
            <line x1="16" y1="13" x2="8" y2="13"></line>
            <line x1="16" y1="17" x2="8" y2="17"></line>
            <polyline points="10 9 9 9 8 9"></polyline>
          </svg>
          <span>ctx-vault</span>
        </div>
        <p className="subtitle">Multimodal Ingestion Interface</p>
      </header>

      <main className="main">
        {/* URL Ingestion Section */}
        <section className="section">
          <h2>Ingest from URL</h2>
          <form onSubmit={handleUrlIngest} className="url-form">
            <div className="input-group">
              <input
                type="url"
                name="url"
                placeholder="https://example.com/article"
                required
                className="url-input"
              />
              <button 
                type="submit" 
                disabled={uploading}
                className="btn btn-primary"
              >
                {uploading ? 'Ingesting...' : 'Ingest URL'}
              </button>
            </div>
          </form>
        </section>

        {/* File Drop Zone */}
        <section className="section">
          <h2>Drag & Drop Files</h2>
          <div
            className={`drop-zone ${dragActive ? 'active' : ''} ${uploading ? 'uploading' : ''}`}
            onDragEnter={handleDrag}
            onDragLeave={handleDrag}
            onDragOver={handleDrag}
            onDrop={handleDrop}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              onChange={handleFileSelect}
              className="file-input"
              id="file-upload"
            />
            <label htmlFor="file-upload" className="drop-label">
              <svg className="drop-icon" width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="17 8 12 3 7 8"></polyline>
                <line x1="12" y1="3" x2="12" y2="15"></line>
              </svg>
              <p className="drop-text">Drag & drop files here, or click to browse</p>
              <p className="drop-hint">Supports: PDF, images, audio, video, HTML</p>
            </label>
          </div>
        </section>

        {/* Results */}
        <section className="section results-section">
          <div className="results-header">
            <h2>Ingestion Results ({results.length})</h2>
            {results.length > 0 && (
              <button 
                onClick={() => setResults([])} 
                className="btn btn-secondary"
              >
                Clear All
              </button>
            )}
          </div>
          
          {results.length === 0 ? (
            <div className="empty-state">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                <polyline points="14 2 14 8 20 8"></polyline>
                <line x1="16" y1="13" x2="8" y2="13"></line>
                <line x1="16" y1="17" x2="8" y2="17"></line>
                <polyline points="10 9 9 9 8 9"></polyline>
              </svg>
              <p>No files ingested yet</p>
              <p className="empty-hint">Drop a file or enter a URL above to get started</p>
            </div>
          ) : (
            <div className="results-list">
              {results.map((result, index) => (
                <div key={index} className={`result-card ${result.status}`}>
                  <div className="result-header">
                    <div className="result-info">
                      <span className={`status-badge ${result.status}`}>
                        {result.status === 'success' ? '✓' : '✕'} {result.status}
                      </span>
                      <span className="result-file">{result.file}</span>
                      <span className="result-type">{result.type || result.mime || 'unknown'}</span>
                    </div>
                    {result.chunks_extracted && (
                      <span className="chunks-count">{result.chunks_extracted} chunks</span>
                    )}
                  </div>
                  
                  {result.status === 'success' && (
                    <div className="result-details">
                      {result.title && <p><strong>Title:</strong> {result.title}</p>}
                      {result.processing_time_ms && (
                        <p><strong>Processing:</strong> {result.processing_time_ms.toFixed(1)}ms</p>
                      )}
                      {result.file_path && (
                        <p><strong>Saved as:</strong> <code>{result.file_path}</code></p>
                      )}
                      {result.tags && result.tags.length > 0 && (
                        <p><strong>Tags:</strong> {result.tags.join(', ')}</p>
                      )}
                    </div>
                  )}
                  
                  {result.status === 'error' && (
                    <div className="error-details">
                      <p><strong>Error:</strong> {result.error}</p>
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>
      </main>

      <footer className="footer">
        <p>ctx-vault v2.0 • Multimodal Knowledge Base for AI Agents</p>
      </footer>
    </div>
  )
}

export default App